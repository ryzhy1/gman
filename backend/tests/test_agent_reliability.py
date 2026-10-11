import io
import json
import os
import queue
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from agent import collect
from agent.collect import Blocked, Rejected, Retryable, Spool, Watcher
from backend.tests.test_api import auth, client, post  # noqa: F401  (pytest fixture)

ROOT = Path(__file__).resolve().parents[2]


def snap(n, instances=None):
    observed = datetime.now(timezone.utc) - timedelta(seconds=100 - n)
    return {"source_id": "node-1", "source_type": "docker-host", "hostname": "host", "errors": [],
            "observed_at": observed.isoformat(), "instances": instances or [], "n": n}


# --- Spool ----------------------------------------------------------------------------------------

def test_spool_bounded_by_count_drops_oldest_and_reports_gap(tmp_path):
    spool = Spool(tmp_path, max_files=3)
    for n in range(5):
        spool.put(snap(n))
    kept = [json.loads(p.read_text()) for p in spool.items()]
    assert [s["n"] for s in kept] == [2, 3, 4]
    assert kept[-1]["errors"] == ["agent spool overflow: dropped 1 oldest unsent snapshot(s)"]
    assert kept[0]["errors"] == []
    assert oct(os.stat(spool.items()[0]).st_mode & 0o777) == "0o600"


def test_spool_bounded_by_bytes_keeps_newest(tmp_path):
    size = len(json.dumps(snap(0)).encode())
    spool = Spool(tmp_path, max_bytes=size * 2 + 200)
    for n in range(4):
        spool.put(snap(n))
        assert sum(p.stat().st_size for p in spool.items()) <= size * 2 + 200
    assert [json.loads(p.read_text())["n"] for p in spool.items()] == [2, 3]
    tiny = Spool(tmp_path / "tiny", max_bytes=1)
    tiny.put(snap(0))
    tiny.put(snap(1))
    assert [json.loads(p.read_text())["n"] for p in tiny.items()] == [1]


def test_spool_order_survives_clock_going_back(tmp_path, monkeypatch):
    monkeypatch.setattr(collect.time, "time_ns", lambda: 5)
    spool = Spool(tmp_path)
    for n in range(3):
        spool.put(snap(n))
    (tmp_path / "leftover.tmp").write_text("partial")
    sent = []
    assert Spool(tmp_path).flush(lambda d: sent.append(d["n"])) == 0
    assert sent == [0, 1, 2]
    assert not list(tmp_path.iterdir())


def test_spool_flush_stops_on_transient_and_drops_rejected(tmp_path):
    spool = Spool(tmp_path)
    for n in range(4):
        spool.put(snap(n))
    (tmp_path / f"{int(spool.items()[0].stem) - 1:020d}.json").write_text("{broken")
    sent = []

    def deliver(data):
        if data["n"] == 1:
            raise Rejected("HTTP 422")
        if data["n"] == 2:
            raise Retryable("HTTP 503")
        sent.append(data["n"])

    assert spool.flush(deliver) == 2
    assert sent == [0]
    assert [json.loads(p.read_text())["n"] for p in spool.items()] == [2, 3]

    def blocked(data):
        raise Blocked("HTTP 401")

    assert spool.flush(blocked) == 2
    assert spool.flush(lambda d: sent.append(d["n"])) == 0
    assert sent == [0, 2, 3]


# --- Retry ----------------------------------------------------------------------------------------

def test_retry_with_exponential_backoff_and_jitter():
    sleeps, attempts = [], []

    def flaky(data):
        attempts.append(data)
        if len(attempts) < 4:
            raise Retryable("timeout")
        return "ok"

    assert collect.send_with_retry("x", flaky, retries=3, base=2, cap=5, sleep=sleeps.append, rng=lambda: 1) == "ok"
    assert sleeps == [2, 4, 5]
    sleeps.clear()
    attempts.clear()
    with pytest.raises(Retryable):
        collect.send_with_retry("x", flaky, retries=2, base=2, cap=30, sleep=sleeps.append, rng=lambda: 0)
    assert len(attempts) == 3 and sleeps == [1, 2]
    assert all(1 <= collect.backoff(1, 2, 30) <= 4 for _ in range(50))


def test_permanent_errors_are_not_retried():
    calls = []

    def rejected(data):
        calls.append(data)
        raise Rejected("HTTP 422")

    with pytest.raises(Rejected):
        collect.send_with_retry("x", rejected, retries=3, sleep=lambda s: pytest.fail("no retry"))
    assert len(calls) == 1


@pytest.mark.parametrize("error,kind", [
    (HTTPError("u", 503, "x", {}, io.BytesIO(b"down")), Retryable),
    (HTTPError("u", 429, "x", {}, io.BytesIO(b"")), Retryable),
    (HTTPError("u", 401, "x", {}, io.BytesIO(b"")), Blocked),
    (HTTPError("u", 422, "x", {}, io.BytesIO(b"bad")), Rejected),
    (URLError("refused"), Retryable),
    (TimeoutError(), Retryable),
])
def test_post_classifies_failures(monkeypatch, error, kind):
    def fail(request, timeout):
        assert request.headers["Authorization"] == "Bearer t"
        raise error

    monkeypatch.setattr(collect, "urlopen", fail)
    with pytest.raises(kind):
        collect.post({"a": 1}, ("https://gman/api/inventory", "t"))


# --- Lock and one-shot runs -----------------------------------------------------------------------

def test_lock_is_exclusive_and_non_blocking(tmp_path):
    first = collect.acquire_lock(tmp_path / "s.lock")
    assert first is not None
    assert collect.acquire_lock(tmp_path / "s.lock") is None
    other = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from agent import collect;"
                            "print(collect.acquire_lock(sys.argv[2]))", str(ROOT), str(tmp_path / "s.lock")],
                           capture_output=True, text=True, check=True)
    assert other.stdout.strip() == "None"
    os.close(first)
    assert collect.acquire_lock(tmp_path / "s.lock") is not None


def test_overlapping_run_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GMAN_URL", "https://gman.example")
    monkeypatch.setenv("GMAN_AGENT_TOKEN", "t")
    monkeypatch.setattr(collect, "collect", lambda *a: pytest.fail("must not collect while locked"))
    held = collect.acquire_lock(tmp_path / "node-1.lock")
    collect.main(["--runtime", "docker", "--source-id", "node-1", "--send", "--state-dir", str(tmp_path)])
    assert "holds the lock" in capsys.readouterr().err
    os.close(held)


def test_cli_validation():
    for argv in (["--runtime", "kubernetes", "--source-id", "c", "--send", "--watch"],
                 ["--runtime", "docker", "--source-id", "c", "--watch"],
                 ["--runtime", "docker", "--source-id", "..", "--send"]):
        with pytest.raises(SystemExit):
            collect.main(argv)


def test_failed_collection_is_not_sent_but_spool_is_flushed(tmp_path):
    spool = Spool(tmp_path)
    spool.put(snap(0))
    sent = []

    def broken():
        raise RuntimeError("docker inspect failed for existing containers")

    with pytest.raises(RuntimeError):
        collect.run_once(broken, spool, lambda d: sent.append(d["n"]))
    assert sent == [0] and not spool.items()


def test_failed_delivery_is_spooled_and_replayed_in_order(tmp_path):
    spool = Spool(tmp_path)

    def down(data):
        raise Retryable("refused")

    with pytest.raises(SystemExit, match="1 snapshot"):
        collect.run_once(lambda: snap(0), spool, down)
    with pytest.raises(SystemExit, match="2 snapshot"):
        collect.run_once(lambda: snap(1), spool, down)
    sent = []
    collect.run_once(lambda: snap(2), spool, lambda d: sent.append(d["n"]))
    assert sent == [0, 1, 2] and not spool.items()


# --- Watch mode -----------------------------------------------------------------------------------

def event(action, cid="c1", **attributes):
    return {"Type": "container", "Action": action, "id": cid, "timeNano": 1_760_000_000_000_000_000,
            "Actor": {"ID": cid, "Attributes": {"name": "job", "image": "busybox", **attributes}}}


def test_debounce_coalesces_bursts_and_caps_delay():
    w = Watcher(0, debounce=2, max_delay=10, interval=60, capture=lambda e, p: {"id": e["id"]})
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
    w.handle({"Type": "network", "Action": "connect"}, 21)
    assert w.deadline() == 80 and not w.seen
    w.handle(event("health_status: unhealthy"), 22)
    assert w.deadline() == 24


def test_short_lived_container_is_reported_once_gone(monkeypatch):
    def inspect(args):
        raise subprocess.CalledProcessError(1, args)  # already auto-removed (--rm)

    monkeypatch.setattr(collect, "command", inspect)
    w = Watcher(0)
    w.handle(event("start", cid="eph", **{"gman.service": "batch", "secret": "x"}), 0)
    w.handle(event("die", cid="eph", exitCode="3"), 0.1)
    seen = w.take(3)
    instance = seen["eph"]
    assert (instance["status"], instance["exit_code"], instance["service_id"]) == ("exited", 3, "batch")
    assert instance["finished_at"] and "x" not in str(instance["origin"])
    first, second = collect.with_ephemeral(snap(0, [{"id": "kept"}]), seen)
    assert [i["id"] for i in first["instances"]] == ["kept", "eph"]
    assert [i["id"] for i in second["instances"]] == ["kept"]
    assert second["observed_at"] > first["observed_at"]
    still_there = snap(0, [{"id": "eph"}])
    assert collect.with_ephemeral(still_there, seen) == [still_there]


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


def test_watch_loop_sends_debounced_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "command", lambda args: (_ for _ in ()).throw(subprocess.CalledProcessError(1, args)))
    clock = Clock()
    lines = FakeLines(clock, [(5, json.dumps(event("start", cid="eph")) + "\n"),
                              (5.2, json.dumps(event("die", cid="eph", exitCode="0")) + "\n"),
                              (5.3, "not json\n"),
                              (6, None)])
    collected, sent = [], []

    def collect_snapshot():
        collected.append(clock.now)
        if len(collected) == 3:
            raise RuntimeError("daemon down")
        return snap(len(collected), [{"id": "kept"}])

    spool = Spool(tmp_path)
    collect.watch(collect_snapshot, spool, lambda d: sent.append([i["id"] for i in d["instances"]]),
                  Watcher(0, debounce=2, interval=60), lines=lines, clock=clock, retry_delay=10, cycles=4)
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

    def popen(args, **kwargs):
        started.append(args)
        if len(started) == 2:
            raise FileNotFoundError("docker")
        return Process('{"Action":"start"}\n')

    lines = queue.Queue()
    collect.docker_events(lines, popen=popen, sleep=lambda s: None, restarts=3)
    assert [lines.get_nowait() for _ in range(lines.qsize())] == ['{"Action":"start"}\n', None, None,
                                                                   '{"Action":"start"}\n', None]
    assert started[0][:2] == ["docker", "events"] and "type=container" in started[0]


def test_replayed_and_ephemeral_snapshots_are_accepted_by_api(client):  # noqa: F811
    seen = {"eph": {"id": "eph", "name": "job", "runtime": "docker", "status": "exited", "exit_code": 0,
                    "service_id": "", "config": {"image": "busybox"}}}
    first = snap(0, [])
    later = snap(1, [{"id": "kept", "name": "api", "runtime": "docker", "status": "running",
                      "config": {"image": "api"}}])
    first.pop("n"), later.pop("n")
    responses = [post(client, d).json()["accepted"] for d in [first, *collect.with_ephemeral(later, seen), first]]
    # Replaying an already delivered (older) snapshot is a harmless no-op, not an error.
    assert responses == [True, True, True, False]
    types = [e["type"] for e in client.get("/api/events", headers=auth("read")).json()["events"]]
    assert types.count("instance-appeared") == 2 and types.count("instance-removed") == 1
