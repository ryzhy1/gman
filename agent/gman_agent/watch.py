"""--watch: debounced snapshots driven by `docker events` or the Kubernetes watch API."""
import json
import queue
import random
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import docker, kubernetes as k8s
from .common import log, newest
from .transport import backoff


# --- Watch mode -----------------------------------------------------------------------------------

# Container events that change what a snapshot shows. exec_* (healthchecks), attach, resize, top
# and similar are ignored: they would trigger a snapshot every healthcheck interval.
WATCH_TRIGGERS = {"create", "start", "restart", "die", "oom", "kill", "stop", "pause", "unpause",
                  "update", "rename", "destroy", "health_status"}
# On these the container is inspected immediately: it may be gone by the next snapshot.
WATCH_CAPTURE = {"start", "die"}


class Watcher:
    """Debounce for `docker events`.

    A snapshot is due after `debounce` s without events, at most `max_delay` s after the first
    pending event, and at least every `interval` s. Containers captured on start/die are kept
    until the next snapshot so that short-lived ones can still be reported.
    """

    def __init__(self, now, debounce=2.0, max_delay=10.0, interval=60.0, capture=docker.capture):
        self.debounce, self.max_delay, self.interval, self.capture = debounce, max_delay, interval, capture
        self.first = self.last = None
        self.next_poll = now  # full snapshot right after start
        self.seen = {}
        # Container ID -> time of the latest `oom` event. Docker clears State.OOMKilled when a restart
        # policy restarts the container, and an OOM kill of a child process does not stop it at all.
        self.ooms = {}

    def handle(self, event, now):
        if event.get("Type", "container") != "container":
            return
        action = (event.get("Action") or event.get("status") or "").split(":")[0].strip()
        if action not in WATCH_TRIGGERS:
            return
        cid = docker.event_id(event)
        if action == "oom" and cid:
            when = docker.event_time(event) or datetime.now(timezone.utc).isoformat()
            self.ooms[cid] = newest(self.ooms.get(cid), when)
        if action in WATCH_CAPTURE and cid:
            self.seen[cid] = self.capture(event, self.seen.get(cid))
        self.trigger(now)

    def trigger(self, now):
        self.last = now
        if self.first is None:
            self.first = now

    def snapshots(self, data, seen):
        """Snapshots to send for one collection: ephemeral containers and OOM times added."""
        result = with_ephemeral(data, seen)
        if self.ooms:
            for instance in {id(i): i for snapshot in result for i in snapshot["instances"]}.values():
                if instance["id"] in self.ooms:
                    instance["last_oom_at"] = newest(instance.get("last_oom_at"), self.ooms[instance["id"]])
            # Reported while the container exists; the API keeps the latest time once it is recorded.
            self.ooms = {cid: at for cid, at in self.ooms.items() if cid in {i["id"] for i in data["instances"]}}
        return result

    def resync(self, now):
        self.next_poll = min(self.next_poll, now)

    def deadline(self):
        if self.last is None:
            return self.next_poll
        return min(self.next_poll, self.last + self.debounce, self.first + self.max_delay)

    def due(self, now):
        return now >= self.deadline()

    def take(self, now):
        seen, self.seen = self.seen, {}
        self.first = self.last = None
        self.next_poll = now + self.interval
        return seen

    def retry(self, seen, now, delay):
        self.seen = {**seen, **self.seen}
        self.next_poll = min(self.next_poll, now + delay)


def with_ephemeral(data, seen):
    """Snapshots to send for one collection.

    Containers seen in events but absent now (removed, e.g. --rm) go into an interim snapshot with
    their last captured state, followed 1 ms later by the real one, so the API records both their
    appearance/exit and their removal.
    """
    present = {i["id"] for i in data["instances"]}
    gone = [instance for cid, instance in seen.items() if cid not in present]
    if not gone:
        return [data]
    interim = dict(data, instances=data["instances"] + gone)
    observed = datetime.fromisoformat(data["observed_at"])
    return [interim, dict(data, observed_at=(observed + timedelta(milliseconds=1)).isoformat())]


def watch_path(api_path, version, timeout):
    separator = "&" if "?" in api_path else "?"
    return (f"{api_path}{separator}watch=1&allowWatchBookmarks=true&resourceVersion={quote(str(version))}"
            f"&timeoutSeconds={int(timeout)}")


def kube_watch(resource, api_path, items, list_=k8s.kube_raw, stream=k8s.kube_stream, sleep=time.sleep, timeout=300,
               rng=random.random, attempts=None):
    """List `api_path`, then watch it from the list's resourceVersion, feeding dicts into `items`.

    {"resource", "type": "SYNC", "items"} after every (re)list, {"resource", "type", "object"} for
    ADDED/MODIFIED/DELETED. BOOKMARK only advances the resourceVersion. A watch the server ends after
    `timeout` resumes from the last resourceVersion; 410 Gone (expired version) relists at once; other
    failures put None (resync) and relist after an exponential backoff. `attempts` bounds the loop for tests.
    """
    version, failures = None, 0
    while attempts is None or attempts > 0:
        if attempts is not None:
            attempts -= 1
        try:
            if version is None:
                listing = list_(api_path)
                version = (listing.get("metadata") or {})["resourceVersion"]
                items.put({"resource": resource, "type": "SYNC", "items": listing.get("items") or []})
            for event in stream(watch_path(api_path, version, timeout), timeout):
                kind, obj = event.get("type"), event.get("object") or {}
                if kind == "ERROR":
                    # The API reports an expired resourceVersion as a Status object inside the stream.
                    if obj.get("code") == 410:
                        raise k8s.Gone()
                    raise RuntimeError(f"watch error {obj.get('code')} {obj.get('reason', '')}")
                version = (obj.get("metadata") or {}).get("resourceVersion") or version
                failures = 0
                if kind in ("ADDED", "MODIFIED", "DELETED"):
                    items.put({"resource": resource, "type": kind, "object": obj})
        except k8s.Gone:
            log(f"{resource} watch: resourceVersion expired (410 Gone); relisting")
            version = None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            delay = backoff(failures, 1.0, 60.0, rng)
            failures += 1
            log(f"{resource} watch failed: {type(error).__name__}; relist in {delay:.1f}s")
            version = None
            items.put(None)
            sleep(delay)


class KubeWatcher(Watcher):
    """Debounce for the Kubernetes watch of pods, pod Events and node Events, with a pod cache built from it.

    Snapshots are built from the cache (nodes are still listed per snapshot). Pods seen in the window are
    kept with their last state, so a pod created and deleted between two snapshots is still reported
    (with_ephemeral). Deleted pods stay known for `keep_deleted` seconds to map their late Events.
    """

    def __init__(self, now, debounce=2.0, max_delay=10.0, interval=60.0, keep_deleted=600.0):
        super().__init__(now, debounce, max_delay, interval, capture=None)
        self.pods, self.deleted, self.events = {}, {}, {}
        self.synced, self.keep_deleted, self.now = False, keep_deleted, now

    def handle(self, item, now):
        resource, kind = item.get("resource"), item.get("type")
        self.now = now
        if resource == "pods":
            if kind == "SYNC":
                fresh = {p["metadata"]["uid"]: p for p in item.get("items") or []}
                # Pods deleted while the watch was down: their Events can still arrive.
                self.deleted.update({uid: (k8s.pod_ref(pod), now) for uid, pod in self.pods.items() if uid not in fresh})
                self.pods, self.synced = fresh, True
            elif kind in ("ADDED", "MODIFIED", "DELETED"):
                pod = item["object"]
                uid = pod["metadata"]["uid"]
                if kind == "DELETED":
                    self.pods.pop(uid, None)
                    self.deleted[uid] = (k8s.pod_ref(pod), now)
                else:
                    self.pods[uid] = pod
                for instance in k8s.pod_instances(pod):
                    self.seen[instance["id"]] = instance
            else:
                return
        elif resource in ("events", "node-events") and kind in ("SYNC", "ADDED", "MODIFIED"):
            fresh = (item.get("items") or []) if kind == "SYNC" else [item["object"]]
            relevant = [e for e in fresh if k8s.relevant_event(e)]
            for event in relevant:
                self.events[(event.get("metadata") or {}).get("uid")] = event
            if not relevant:
                return
        else:
            return
        self.trigger(now)

    def pod_list(self):
        """Cached pods, or None before the first list (the snapshot then lists them itself)."""
        return list(self.pods.values()) if self.synced else None

    def snapshots(self, data, seen):
        self.deleted = {uid: v for uid, v in self.deleted.items() if self.now - v[1] <= self.keep_deleted}
        refs = {uid: ref for uid, (ref, _) in self.deleted.items()}
        refs.update({uid: k8s.pod_ref(pod) for uid, pod in self.pods.items()})
        # Events not mapped to a known pod now are dropped: a relist brings back those still stored.
        events = k8s.kube_events(self.events.values(), refs)
        self.events = {}
        result = [dict(snapshot, events=[]) for snapshot in with_ephemeral(data, seen)]
        result[-1]["events"] = events
        return result


def watch(collect_snapshot, spool, deliver, watcher, lines=None, clock=time.monotonic,
          retry_delay=10.0, cycles=None, feeds=(docker.docker_events,)):
    """Debounced snapshots driven by a stream. Queue items: a JSON line (docker events), a dict (Kubernetes
    watch, see kube_watch) or None for a broken stream (resync)."""
    if lines is None:
        lines = queue.Queue()
        for feed in feeds:
            threading.Thread(target=feed, args=(lines,), daemon=True).start()
    done = 0
    while cycles is None or done < cycles:
        try:
            line = lines.get(timeout=max(0.0, watcher.deadline() - clock()))
        except queue.Empty:
            line = ""
        now = clock()
        if line is None:
            watcher.resync(now)  # events may have been missed while the stream was down
        elif isinstance(line, dict):
            watcher.handle(line, now)
        elif line.strip():
            try:
                watcher.handle(json.loads(line), now)
            except ValueError:
                log("ignoring malformed docker event")
        if not watcher.due(now):
            continue
        done += 1
        seen = watcher.take(now)
        try:
            data = collect_snapshot()
        except Exception as error:
            # As in one-shot mode, a failed collection is never sent as an empty snapshot.
            log(f"collection failed: {type(error).__name__}; retry in {retry_delay:g}s")
            watcher.retry(seen, now, retry_delay)
            continue
        for snapshot in watcher.snapshots(data, seen):
            spool.put(snapshot)
        spool.flush(deliver)
