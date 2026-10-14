"""Kubernetes --watch, pod Events and Docker OOM capture in the agent (fixtures, no cluster needed)."""
import io
import json
import queue
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from agent.gman_agent import cli, common, docker, kubernetes as k8s, watch
from agent.gman_agent.watch import KubeWatcher, Watcher
from agent.gman_agent.transport import Spool
from backend.tests.test_agent_reliability import Clock, FakeLines, event as docker_event, snap


def pod(uid, name="api-1", service="api", rv="1", phase="Running", last=None, containers=("app",)):
    statuses = [{"name": c, "ready": True, "restartCount": 0, "state": {"running": {"startedAt": "2026-10-11T10:00:00Z"}},
                 "imageID": "sha256:1", **({"lastState": {"terminated": last}} if last else {})} for c in containers]
    return {"metadata": {"uid": uid, "name": name, "namespace": "prod", "resourceVersion": rv,
                         "creationTimestamp": "2026-10-11T09:59:00Z",
                         "labels": {"gman.service": service, "gman.environment": "prod"} if service else {}},
            "spec": {"nodeName": "node-1", "containers": [{"name": c, "image": "api:1"} for c in containers]},
            "status": {"phase": phase, "containerStatuses": statuses}}


def kube_ev(uid, reason, pod_uid, message="", field_path="spec.containers{app}", count=1,
            last="2026-10-11T10:05:00Z", **extra):
    return {"metadata": {"uid": uid, "resourceVersion": "9"}, "reason": reason, "type": "Warning", "count": count,
            "message": message, "firstTimestamp": "2026-10-11T10:00:00Z", "lastTimestamp": last,
            "source": {"component": "kubelet", "host": "node-1"},
            "involvedObject": {"kind": "Pod", "namespace": "prod", "name": "api-1", "uid": pod_uid,
                               "fieldPath": field_path}, **extra}


def node_ev(uid, reason, message="", node="node-1", component="kubelet", last="2026-10-11T10:03:00Z", **extra):
    """A node Event as kubelet / node-problem-detector write it: namespace default, node UID = node name."""
    return {"metadata": {"uid": uid, "namespace": "default", "resourceVersion": "9"}, "reason": reason,
            "type": "Warning", "count": 1, "message": message, "firstTimestamp": "2026-10-11T10:00:00Z",
            "lastTimestamp": last, "source": {"component": component, "host": node},
            "involvedObject": {"kind": "Node", "name": node, "uid": node}, **extra}


# --- Events mapping -------------------------------------------------------------------------------

def refs(*pods):
    return {p["metadata"]["uid"]: k8s.pod_ref(p) for p in pods}


def test_event_is_mapped_to_instance_without_message():
    secret = "Liveness probe failed: HTTP probe failed with statuscode: 500, body: password=hunter2"
    mapped = k8s.kube_event(kube_ev("e1", "Unhealthy", "p1", secret, count=4), refs(pod("p1")))
    assert mapped == {"uid": "e1", "reason": "Unhealthy", "type": "Warning", "count": 4,
                      "first_at": "2026-10-11T10:00:00Z", "last_at": "2026-10-11T10:05:00Z",
                      "service_id": "api", "environment": "prod", "namespace": "prod", "pod": "api-1",
                      "container": "app", "instance_id": "p1:app", "instance_name": "api-1/app",
                      "component": "kubelet", "detail": "liveness probe failed"}
    assert "hunter2" not in json.dumps(mapped) and "message" not in mapped


@pytest.mark.parametrize("reason,message,detail", [
    ("FailedScheduling", "0/3 nodes are available: 1 Insufficient nvidia.com/gpu, 2 node(s) had untolerated taint "
                         "{dedicated: secret-team}. preemption: 0/3 nodes are available",
     "0/3 nodes are available, 1 Insufficient nvidia.com/gpu, 2 node(s) had untolerated taint, 0/3 nodes are available"),
    ("Evicted", "The node was low on resource: memory. Threshold quantity: 100Mi, available: 50Mi.", "node low on memory"),
    ("BackOff", "Back-off restarting failed container app in pod api-1_prod(p1)", "restarting failed container"),
    ("Failed", 'Failed to pull image "registry.local/api:1": pull access denied, token=abc', "image pull failed"),
    ("Failed", "Error: ImagePullBackOff", "ImagePullBackOff"),
    ("Killing", "Stopping container app", ""),
    ("OOMKilling", "Memory cgroup out of memory: Killed process 1234 (python)", ""),
])
def test_event_detail_keeps_only_fixed_phrases(reason, message, detail):
    assert k8s.event_detail(reason, message) == detail


def test_events_filtered_by_reason_kind_and_known_service():
    p1, unlabeled = pod("p1"), pod("p2", name="tmp", service="")
    events = [kube_ev("e1", "BackOff", "p1", last="2026-10-11T10:01:00Z"),
              kube_ev("e2", "Pulled", "p1"),  # normal noise
              kube_ev("e3", "Killing", "p2"),  # pod without gman.service
              kube_ev("e4", "Killing", "gone"),  # unknown pod
              {**kube_ev("e5", "FailedScheduling", "p1", field_path=""), "lastTimestamp": None,
               "eventTime": "2026-10-11T10:07:00.123456Z", "series": {"count": 7,
                                                                    "lastObservedTime": "2026-10-11T10:09:00.5Z"}},
              {**kube_ev("e6", "Evicted", "p1"), "involvedObject": {"kind": "Node", "name": "node-1"}}]
    mapped = k8s.kube_events(events, refs(p1, unlabeled))
    assert [(m["uid"], m["count"], m["instance_id"]) for m in mapped] == [("e5", 7, ""), ("e1", 1, "p1:app")]
    assert mapped[0]["last_at"] == "2026-10-11T10:09:00.5Z" and mapped[0]["instance_name"] == "api-1"


def test_one_shot_kubernetes_collects_events_and_tolerates_missing_rbac(monkeypatch):
    calls, forbidden = [], set()

    def kubectl(args):
        calls.append(args)
        if args[:2] == ["kubectl", "get"] and args[2] == "events":
            kind = args[args.index("--field-selector") + 1].split("=")[1]
            if kind in forbidden:
                raise subprocess.CalledProcessError(1, args)
            return json.dumps({"items": [kube_ev("e1", "BackOff", "p1")] if kind == "Pod" else [node_ev("n1", "Rebooted")]})
        return json.dumps({"items": [pod("p1")] if args[2] == "pods" else []})

    monkeypatch.setattr(common, "command", kubectl)
    data = cli.collect("kubernetes", "cluster-01")
    assert [e["uid"] for e in data["events"]] == ["e1", "n1"] and not data["errors"]
    assert "involvedObject.kind=Pod" in calls[2] and "involvedObject.kind=Node" in calls[3]
    forbidden.add("Node")
    data = cli.collect("kubernetes", "cluster-01")
    assert [e["uid"] for e in data["events"]] == ["e1"]
    assert data["errors"] == ["kubernetes node events unavailable: CalledProcessError"]
    forbidden.add("Pod")
    data = cli.collect("kubernetes", "cluster-01")
    assert data["events"] == [] and len(data["errors"]) == 2 and len(data["instances"]) == 1


def test_pod_last_oom_from_termination_state():
    oom = {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-10-11T10:04:00Z"}
    assert k8s.pod_instances(pod("p1", last=oom))[0]["last_oom_at"] == "2026-10-11T10:04:00Z"
    assert k8s.pod_instances(pod("p1", last={**oom, "reason": "Error"}))[0]["last_oom_at"] is None


# --- Watch stream ---------------------------------------------------------------------------------

def watch_line(kind, obj):
    return {"type": kind, "object": obj}


def test_kube_watch_list_watch_bookmark_410_and_backoff():
    lists, paths, sleeps = [], [], []
    p1 = pod("p1", rv="11")
    streams = [
        # first watch: changes, a bookmark, then the server ends the watch (timeoutSeconds)
        [watch_line("ADDED", p1), watch_line("MODIFIED", pod("p1", rv="12")),
         watch_line("BOOKMARK", {"kind": "Pod", "metadata": {"resourceVersion": "15"}}),
         watch_line("DELETED", pod("p1", rv="16"))],
        # resumed watch: the version was compacted meanwhile
        [watch_line("ERROR", {"kind": "Status", "code": 410, "reason": "Expired"})],
        # after the relist: kubectl fails (e.g. API server restart)
        subprocess.CalledProcessError(1, "kubectl"),
        [],
    ]

    def list_(path):
        lists.append(path)
        return {"metadata": {"resourceVersion": str(10 * len(lists))}, "items": [p1] if len(lists) == 1 else []}

    def stream(path, timeout):
        paths.append(path)
        item = streams.pop(0)
        if isinstance(item, Exception):
            raise item
        yield from item

    items = queue.Queue()
    watch.kube_watch("pods", "/api/v1/pods", items, list_=list_, stream=stream, sleep=sleeps.append,
                     timeout=300, rng=lambda: 0.0, attempts=4)
    got = [items.get_nowait() for _ in range(items.qsize())]
    assert [g and (g["type"], g.get("object", {}).get("metadata", {}).get("resourceVersion")) for g in got] == [
        ("SYNC", None), ("ADDED", "11"), ("MODIFIED", "12"), ("DELETED", "16"),
        ("SYNC", None),  # 410 Gone: relisted at once, no resync marker
        None,  # stream failure: resync marker, then relist after backoff
        ("SYNC", None)]
    assert got[0]["items"] == [p1]
    assert paths[0] == "/api/v1/pods?watch=1&allowWatchBookmarks=true&resourceVersion=10&timeoutSeconds=300"
    assert "resourceVersion=16&" in paths[1]  # resumed from the last seen version, bookmark included
    assert "resourceVersion=20&" in paths[2] and "resourceVersion=30&" in paths[3]
    assert len(lists) == 3 and sleeps == [0.5]


def test_kube_stream_parses_kubectl_output_and_fails_on_exit_code(monkeypatch):
    class Process:
        def __init__(self, out, code):
            self.stdout, self.code, self.killed = io.StringIO(out), code, False

        def wait(self):
            return self.code

        def poll(self):
            return self.code

    runs = []

    def popen(args, **kwargs):
        runs.append(args)
        return Process(json.dumps(watch_line("ADDED", {"a": 1})) + "\n\n" +
                       json.dumps(watch_line("BOOKMARK", {})) + "\n", len(runs) - 1)

    monkeypatch.setattr(k8s.subprocess, "Popen", popen)
    assert [e["type"] for e in k8s.kube_stream("/api/v1/pods?watch=1", 300)] == ["ADDED", "BOOKMARK"]
    assert runs[0] == ["kubectl", "get", "--raw", "/api/v1/pods?watch=1"]
    with pytest.raises(subprocess.CalledProcessError):
        list(k8s.kube_stream("/api/v1/pods?watch=1", 300))


def test_kube_stream_in_cluster_410_is_gone(monkeypatch, tmp_path):
    (tmp_path / "token").write_text("t")
    (tmp_path / "ca.crt").write_text("")
    monkeypatch.setattr(k8s, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setattr(k8s.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("kubectl")))
    monkeypatch.setattr(k8s.ssl, "create_default_context", lambda cafile: None)

    def urlopen(request, timeout, context):
        raise HTTPError(request.full_url, 410, "Gone", {}, io.BytesIO(b""))

    monkeypatch.setattr(k8s, "urlopen", urlopen)
    with pytest.raises(k8s.Gone):
        list(k8s.kube_stream("/api/v1/pods?watch=1", 300))


# --- KubeWatcher ----------------------------------------------------------------------------------

def test_kube_watcher_reports_pod_deleted_between_snapshots_and_its_events():
    w = KubeWatcher(0, debounce=2, max_delay=10, interval=60)
    assert w.pod_list() is None  # before the first list the snapshot lists pods itself
    w.handle({"resource": "pods", "type": "SYNC", "items": [pod("p1")]}, 0)
    w.take(0)
    assert [p["metadata"]["uid"] for p in w.pod_list()] == ["p1"]
    w.handle({"resource": "events", "type": "ADDED", "object": kube_ev("n1", "Pulled", "p1")}, 1)
    w.handle({"resource": "events", "type": "DELETED", "object": kube_ev("e0", "Killing", "p1")}, 1)
    assert w.deadline() == 60  # irrelevant Events do not trigger a snapshot
    job = pod("j1", name="job-1", service="batch")
    w.handle({"resource": "pods", "type": "ADDED", "object": job}, 5)
    w.handle({"resource": "events", "type": "ADDED", "object": kube_ev("e1", "BackOff", "j1")}, 5.5)
    w.handle({"resource": "pods", "type": "DELETED", "object": job}, 6)
    w.handle({"resource": "bogus", "type": "ADDED", "object": {}}, 6)
    assert w.deadline() == 8
    seen = w.take(8)
    data = {"source_id": "cluster-01", "observed_at": datetime.now(timezone.utc).isoformat(),
            "instances": [i for p in w.pod_list() for i in k8s.pod_instances(p)], "events": []}
    interim, final = w.snapshots(data, seen)
    assert [i["id"] for i in interim["instances"]] == ["p1:app", "j1:app"]
    assert [i["id"] for i in final["instances"]] == ["p1:app"]
    # the Event of the deleted pod is still mapped, and only sent once
    assert interim["events"] == [] and [(e["uid"], e["service_id"]) for e in final["events"]] == [("e1", "batch")]
    assert w.snapshots(data, {})[0]["events"] == []


def test_kube_watcher_forgets_deleted_pods_after_a_while():
    w = KubeWatcher(0, keep_deleted=600)
    w.handle({"resource": "pods", "type": "SYNC", "items": [pod("p1")]}, 0)
    w.handle({"resource": "pods", "type": "SYNC", "items": []}, 10)  # deleted while the watch was down
    w.handle({"resource": "events", "type": "SYNC", "items": [kube_ev("e1", "Killing", "p1")]}, 20)
    data = {"observed_at": datetime.now(timezone.utc).isoformat(), "instances": []}
    assert [e["uid"] for e in w.snapshots(data, {})[-1]["events"]] == ["e1"]
    w.handle({"resource": "events", "type": "MODIFIED", "object": kube_ev("e1", "Killing", "p1", count=2)}, 700)
    assert w.snapshots(data, {})[-1]["events"] == []


def test_kube_watch_loop_sends_snapshots_from_cache(tmp_path, monkeypatch):
    nodes_listed = []
    monkeypatch.setattr(k8s, "kube_list", lambda args, path: nodes_listed.append(args) or {"items": []})
    clock = Clock()
    w = KubeWatcher(0, debounce=2, interval=60)
    lines = FakeLines(clock, [(1, {"resource": "pods", "type": "SYNC", "items": [pod("p1")]}),
                              (4, {"resource": "pods", "type": "MODIFIED", "object": pod("p1", phase="Failed")}),
                              (4.5, {"resource": "events", "type": "ADDED", "object": kube_ev("e1", "BackOff", "p1")})])
    sent = []
    watch.watch(lambda: cli.collect("kubernetes", "cluster-01", pods=w.pod_list(), events=[]), Spool(tmp_path),
                sent.append, w, lines=lines, clock=clock, cycles=3)
    # t=0: no cache yet -> pods listed; t=3: from the SYNC; t=6.5: change + Event
    assert nodes_listed[0] == ["get", "pods", "--all-namespaces"]
    assert all(args == ["get", "nodes"] for args in nodes_listed[1:]) and len(nodes_listed) == 4
    assert [len(s["instances"]) for s in sent] == [0, 1, 1]
    assert [[e["uid"] for e in s["events"]] for s in sent] == [[], [], ["e1"]]


# --- Docker OOM -----------------------------------------------------------------------------------

def test_docker_oom_event_sets_last_oom_at_after_restart():
    w = Watcher(0, capture=lambda e, p: None)
    w.take(0)
    oom = docker_event("oom", cid="c1")
    oom["timeNano"] = int(datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc).timestamp() * 1e9)
    w.handle(oom, 1)
    assert w.deadline() == 3  # an OOM kill triggers a snapshot
    seen = w.take(3)
    # The restart policy already restarted it: inspect shows no OOMKilled.
    running = {"id": "c1", "status": "running", "reason": "", "last_oom_at": None}
    older = {"id": "c2", "last_oom_at": "2026-10-11T09:00:00.000000001Z"}
    [snapshot] = w.snapshots(snap(0, [running, older]), seen)
    assert snapshot["instances"][0]["last_oom_at"] == "2026-10-11T10:00:00+00:00"
    assert snapshot["instances"][1]["last_oom_at"] == "2026-10-11T09:00:00.000000001Z"
    # kept while the container exists, so a later snapshot reports it too
    assert w.snapshots(snap(1, [dict(running)]), {})[0]["instances"][0]["last_oom_at"] == "2026-10-11T10:00:00+00:00"
    w.snapshots(snap(2, []), {})
    assert w.ooms == {}


def test_docker_inspect_oom_killed_sets_last_oom_at():
    c = {"Id": "c1", "Name": "/w", "Config": {"Image": "w", "Labels": {}}, "HostConfig": {},
         "State": {"Status": "exited", "OOMKilled": True, "ExitCode": 137, "FinishedAt": "2026-10-11T10:00:00.5Z"}}
    assert docker.docker_instance(c)["last_oom_at"] == "2026-10-11T10:00:00.5Z"
    c["State"].update(OOMKilled=False)
    assert docker.docker_instance(c)["last_oom_at"] is None


def test_parse_time_and_newest():
    assert common.parse_time("2026-10-11T10:00:00.123456789Z") == datetime(2026, 10, 11, 10, 0, 0, 123456,
                                                                           tzinfo=timezone.utc)
    assert common.parse_time("0001-01-01T00:00:00Z") is None and common.parse_time("bogus") is None
    assert common.newest(None, "2026-10-11T10:00:00Z", "2026-10-11T12:00:00+02:00") == "2026-10-11T10:00:00Z"
    assert common.newest(None, "") is None
    later = (datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=1)).isoformat()
    assert common.newest("2026-10-11T10:00:00Z", later) == later
