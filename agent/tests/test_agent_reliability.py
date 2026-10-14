import io
import json
import logging
import os
import queue
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import collect
from collection.orchestrator import CollectionOrchestrator
from conftest import collection, runtime, settings
from delivery.adapters.inventory_api import InventoryApi
from delivery.adapters.run_lock import RunLock
from delivery.adapters.spool_directory import SpoolDirectory
from delivery.entity import (BlockedError, RejectedError, RetryableError, RetryPolicy, SpoolLimits,
                             SubmissionPendingError)
from delivery.orchestrator import DeliveryDependencies, DeliveryOrchestrator
from delivery.services.delivery_retry import DeliveryRetry, backoff
from delivery.services.run_guard import RunGuard
from delivery.services.snapshot_spool import SnapshotSpool
from conftest import FakeRunner
from runtime_access.docker_cli import DockerCli, parse_event
from watching.adapters.docker_event_feed import DockerEventFeed
from watching.entity import SnapshotDocument, WatchTimings
from watching.orchestrator import WatchingDependencies, WatchingOrchestrator
from watching.services.docker_watcher import DockerWatcher
from watching.services.ephemeral_snapshots import with_ephemeral
from watching.services.event_inbox import EventInbox

SRC = Path(__file__).resolve().parents[1] / "src"


def snap(n, instances=None):
    observed = datetime.now(timezone.utc) - timedelta(seconds=100 - n)
    return {"source_id": "node-1", "source_type": "docker-host", "hostname": "host", "errors": [],
            "observed_at": observed.isoformat(), "instances": instances or [], "n": n}


def make_spool(path, max_files=100, max_bytes=50 * 1024 ** 2, **kwargs):
    return SnapshotSpool(SpoolDirectory(path), SpoolLimits(max_files=max_files, max_bytes=max_bytes), **kwargs)


class FakeApi:
    def __init__(self, deliver):
        self.post = deliver


def retry(deliver, retries=3, base=2.0, cap=30.0, **kwargs):
    return DeliveryRetry(FakeApi(deliver), RetryPolicy(retries=retries, base_seconds=base, cap_seconds=cap), **kwargs)


def delivery(path, spool, deliver):
    dependencies = DeliveryDependencies(run_guard=RunGuard(RunLock(), path / "x.lock"), spool=spool,
                                        retry=retry(deliver, retries=0))
    return DeliveryOrchestrator(dependencies)


def ephemeral(snapshot, seen):
    return [item.document for item in with_ephemeral(SnapshotDocument(snapshot), seen)]


class FakeSource:
    def __init__(self, snapshot):
        self.snapshot = snapshot


def watching(watcher, lines, clock, source):
    inbox = EventInbox([], clock, items=lines)
    return WatchingOrchestrator(WatchingDependencies(watcher=watcher, inbox=inbox, source=source))


# --- Spool ----------------------------------------------------------------------------------------

def test_spool_bounded_by_count_drops_oldest_and_reports_gap(tmp_path):
    spool = make_spool(tmp_path, max_files=3)
    for n in range(5):
        spool.put(snap(n))
    kept = [json.loads(p.read_text()) for p in spool.items()]
    assert [s["n"] for s in kept] == [2, 3, 4]
    assert kept[-1]["errors"] == ["agent spool overflow: dropped 1 oldest unsent snapshot(s)"]
    assert kept[0]["errors"] == []
    assert oct(os.stat(spool.items()[0]).st_mode & 0o777) == "0o600"


def test_spool_bounded_by_bytes_keeps_newest(tmp_path):
    size = len(json.dumps(snap(0)).encode())
    spool = make_spool(tmp_path, max_bytes=size * 2 + 200)
    for n in range(4):
        spool.put(snap(n))
        assert sum(p.stat().st_size for p in spool.items()) <= size * 2 + 200
    assert [json.loads(p.read_text())["n"] for p in spool.items()] == [2, 3]
    tiny = make_spool(tmp_path / "tiny", max_bytes=1)
    tiny.put(snap(0))
    tiny.put(snap(1))
    assert [json.loads(p.read_text())["n"] for p in tiny.items()] == [1]


def test_spool_order_survives_clock_going_back(tmp_path):
    spool = make_spool(tmp_path, clock_ns=lambda: 5)
    for n in range(3):
        spool.put(snap(n))
    (tmp_path / "leftover.tmp").write_text("partial")
    sent = []
    assert make_spool(tmp_path).flush(lambda d: sent.append(d["n"])) == 0
    assert sent == [0, 1, 2]
    assert not list(tmp_path.iterdir())


def test_spool_flush_stops_on_transient_and_drops_rejected(tmp_path):
    spool = make_spool(tmp_path)
    for n in range(4):
        spool.put(snap(n))
    (tmp_path / f"{int(spool.items()[0].stem) - 1:020d}.json").write_text("{broken")
    sent = []

    def deliver(data):
        if data["n"] == 1:
            raise RejectedError("HTTP 422")
        if data["n"] == 2:
            raise RetryableError("HTTP 503")
        sent.append(data["n"])

    assert spool.flush(deliver) == 2
    assert sent == [0]
    assert [json.loads(p.read_text())["n"] for p in spool.items()] == [2, 3]

    def blocked(data):
        raise BlockedError("HTTP 401")

    assert spool.flush(blocked) == 2
    assert spool.flush(lambda d: sent.append(d["n"])) == 0
    assert sent == [0, 2, 3]


# --- Retry ----------------------------------------------------------------------------------------

def test_retry_with_exponential_backoff_and_jitter():
    sleeps, attempts = [], []

    def flaky(data):
        attempts.append(data)
        if len(attempts) < 4:
            raise RetryableError("timeout")
        return "ok"

    retry(flaky, retries=3, base=2, cap=5, sleep=sleeps.append, rng=lambda: 1).deliver("x")
    assert len(attempts) == 4 and sleeps == [2, 4, 5]
    sleeps.clear()
    attempts.clear()
    with pytest.raises(RetryableError):
        retry(flaky, retries=2, base=2, cap=30, sleep=sleeps.append, rng=lambda: 0).deliver("x")
    assert len(attempts) == 3 and sleeps == [1, 2]
    assert all(1 <= backoff(1, 2, 30) <= 4 for _ in range(50))


def test_permanent_errors_are_not_retried():
    calls = []

    def rejected(data):
        calls.append(data)
        raise RejectedError("HTTP 422")

    with pytest.raises(RejectedError):
        retry(rejected, retries=3, sleep=lambda s: pytest.fail("no retry")).deliver("x")
    assert len(calls) == 1


@pytest.mark.parametrize("error,kind", [
    (HTTPError("u", 503, "x", {}, io.BytesIO(b"down")), RetryableError),
    (HTTPError("u", 429, "x", {}, io.BytesIO(b"")), RetryableError),
    (HTTPError("u", 401, "x", {}, io.BytesIO(b"")), BlockedError),
    (HTTPError("u", 422, "x", {}, io.BytesIO(b"bad")), RejectedError),
    (URLError("refused"), RetryableError),
    (TimeoutError(), RetryableError),
])
def test_post_classifies_failures(error, kind):
    def fail(request, timeout):
        assert request.get_header("Authorization") is None
        raise error

    with pytest.raises(kind):
        InventoryApi("https://gman/api/inventory", opener=fail).post({"a": 1})


# --- Lock and one-shot runs -----------------------------------------------------------------------

def test_lock_is_exclusive_and_non_blocking(tmp_path):
    lock = RunLock()
    first = lock.acquire(tmp_path / "s.lock")
    assert first is not None
    assert lock.acquire(tmp_path / "s.lock") is None
    other = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                            "from delivery.adapters.run_lock import RunLock; print(RunLock().acquire(sys.argv[2]))",
                            str(SRC), str(tmp_path / "s.lock")], capture_output=True, text=True, check=True)
    assert other.stdout.strip() == "None"
    os.close(first)
    assert lock.acquire(tmp_path / "s.lock") is not None


def test_overlapping_run_exits_cleanly(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    for method in ("snapshot", "docker_snapshot", "docker_snapshot_with_ooms"):
        monkeypatch.setattr(CollectionOrchestrator, method, lambda self: pytest.fail("must not collect while locked"))
    held = RunLock().acquire(tmp_path / "node-1.lock")
    collect.main(["--runtime", "docker", "--source-id", "node-1", "--send", "--state-dir", str(tmp_path)],
                 settings(gman_url="https://gman.example"), runtime())
    assert "holds the lock" in caplog.text
    os.close(held)


def test_cli_validation():
    for argv in (["--runtime", "kubernetes-node", "--node", "n", "--cluster", "c", "--source-id", "c.n", "--send",
                  "--watch"],
                 ["--runtime", "kubernetes", "--source-id", "c", "--watch"],
                 ["--runtime", "docker", "--source-id", "c", "--watch"],
                 ["--runtime", "docker", "--source-id", "..", "--send"]):
        with pytest.raises(SystemExit):
            collect.main(argv, settings(), runtime())


def test_failed_collection_is_not_sent_but_spool_is_flushed(tmp_path):
    spool = make_spool(tmp_path)
    spool.put(snap(0))
    sent = []

    def broken():
        raise RuntimeError("docker inspect failed for existing containers")

    with pytest.raises(RuntimeError):
        delivery(tmp_path, spool, lambda d: sent.append(d["n"])).run_once(broken)
    assert sent == [0] and not spool.items()


def test_failed_delivery_is_spooled_and_replayed_in_order(tmp_path):
    spool = make_spool(tmp_path)

    def down(data):
        raise RetryableError("refused")

    with pytest.raises(SubmissionPendingError, match="1 snapshot"):
        delivery(tmp_path, spool, down).run_once(lambda: snap(0))
    with pytest.raises(SubmissionPendingError, match="2 snapshot"):
        delivery(tmp_path, spool, down).run_once(lambda: snap(1))
    sent = []
    delivery(tmp_path, spool, lambda d: sent.append(d["n"])).run_once(lambda: snap(2))
    assert sent == [0, 1, 2] and not spool.items()


# --- Watch mode -----------------------------------------------------------------------------------

def raw_event(action, cid="c1", **attributes):
    return {"Type": "container", "Action": action, "id": cid, "timeNano": 1_760_000_000_000_000_000,
            "Actor": {"ID": cid, "Attributes": {"name": "job", "image": "busybox", **attributes}}}


def event(action, cid="c1", **attributes):
    return parse_event(json.dumps(raw_event(action, cid, **attributes)))


def test_debounce_coalesces_bursts_and_caps_delay():
    w = DockerWatcher(0, WatchTimings(debounce=2, max_delay=10, interval=60), lambda e, p: {"id": e.container_id})
    w.take(0)
    assert w.deadline() == 60
    w.handle(event("start"), 1)
    w.handle(event("die"), 2.5)
    assert not w.due(4.4) and w.due(4.5)
    w.take(4.5)
    for t in range(10, 25):  # a restart loop never goes quiet
        w.handle(event("restart"), t)
    assert w.deadline() == 20
    w.take(20)
    w.handle(event("exec_start: sh -c healthcheck"), 21)
    w.handle(parse_event('{"Type": "network", "Action": "connect"}'), 21)
    assert w.deadline() == 80 and not w.seen
    w.handle(event("health_status: unhealthy"), 22)
    assert w.deadline() == 24


def test_short_lived_container_is_reported_once_gone():
    def inspect(args):
        raise subprocess.CalledProcessError(1, args)  # already auto-removed (--rm)

    w = DockerWatcher(0, WatchTimings(), collection(handler=inspect).capture_container)
    w.handle(event("start", cid="eph", **{"gman.service": "batch", "secret": "x"}), 0)
    w.handle(event("die", cid="eph", exitCode="3"), 0.1)
    seen = w.take(3)
    instance = seen["eph"]
    assert (instance["status"], instance["exit_code"], instance["service_id"]) == ("exited", 3, "batch")
    assert instance["finished_at"] and "x" not in str(instance["origin"])
    first, second = ephemeral(snap(0, [{"id": "kept"}]), seen)
    assert [i["id"] for i in first["instances"]] == ["kept", "eph"]
    assert [i["id"] for i in second["instances"]] == ["kept"]
    assert second["observed_at"] > first["observed_at"]
    still_there = snap(0, [{"id": "eph"}])
    assert ephemeral(still_there, seen) == [still_there]


class FakeLines:
    """Queue stand-in: events at fixed times; waiting advances a fake clock instead of sleeping."""

    def __init__(self, clock, items):
        self.clock, self.items = clock, list(items)

    def get(self, timeout):
        if self.items and self.items[0][0] <= self.clock.now + timeout:
            at, line = self.items.pop(0)
            self.clock.now = max(self.clock.now, at)
            return line
        self.clock.now += timeout
        raise queue.Empty


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_watch_loop_sends_debounced_snapshots(tmp_path):
    failing = collection(handler=lambda args: (_ for _ in ()).throw(subprocess.CalledProcessError(1, args)))
    clock = Clock()
    lines = FakeLines(clock, [(5, event("start", cid="eph")),
                              (5.2, event("die", cid="eph", exitCode="0")),
                              (6, None)])
    collected, sent = [], []

    def collect_snapshot():
        collected.append(clock.now)
        if len(collected) == 3:
            raise RuntimeError("daemon down")
        return snap(len(collected), [{"id": "kept"}])

    spool = make_spool(tmp_path)
    watcher = DockerWatcher(0, WatchTimings(debounce=2, interval=60, retry_delay=10), failing.capture_container)
    sink = delivery(tmp_path, spool, lambda d: sent.append([i["id"] for i in d["instances"]]))
    watching(watcher, lines, clock, FakeSource(collect_snapshot)).watch(sink.submit, cycles=4)
    # initial snapshot, stream break at 6 forces a resync (the burst is folded into it),
    # failed collection at the next deadline is not sent, the retry 10 s later is.
    assert collected == [0, 6, 66, 76]
    assert sent == [["kept"], ["kept", "eph"], ["kept"], ["kept"]]
    assert not spool.items()


def test_docker_events_stream_restarts_and_signals_gap():
    class Process:
        def __init__(self, out):
            self.stdout = io.StringIO(out)

        def wait(self):
            return 0

    started = []

    class StreamingRunner(FakeRunner):
        def stream(self, args):
            started.append(args)
            if len(started) == 2:
                raise FileNotFoundError("docker")
            return Process('{"Action":"start"}\nnot json\n\n')

    lines = queue.Queue()
    DockerEventFeed(DockerCli(StreamingRunner()), sleep=lambda s: None).run(lines, restarts=3)
    start = parse_event('{"Action":"start"}')
    # A malformed line is skipped; a broken or unavailable stream is marked with None (resync).
    assert [lines.get_nowait() for _ in range(lines.qsize())] == [start, None, None, start, None]
    assert started[0][:2] == ["docker", "events"] and "type=container" in started[0]
