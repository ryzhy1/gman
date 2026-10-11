"""Kubernetes Events, OOM time and history gaps in the API (ingest, history, /metrics)."""
from datetime import datetime, timedelta, timezone

from backend.tests.test_api import auth, client, post  # noqa: F401  (pytest fixture)

T0 = datetime.now(timezone.utc) - timedelta(hours=2)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def instance(iid="c1", service="api", **extra):
    return {"id": iid, "name": iid, "runtime": "docker", "status": "running", "service_id": service,
            "config": {"image": "api:1"}, **extra}


def snapshot(seconds, instances=(), **extra):
    return {"source_id": "node-1", "source_type": "docker-host", "hostname": "host", "observed_at": at(seconds),
            "instances": list(instances), **extra}


def kube_event(count=1, last=60, uid="e1", reason="BackOff"):
    return {"uid": uid, "reason": reason, "type": "Warning", "count": count, "first_at": at(0), "last_at": at(last),
            "service_id": "api", "environment": "prod", "namespace": "prod", "pod": "api-1", "container": "app",
            "instance_id": "p1:app", "instance_name": "api-1/app", "component": "kubelet",
            "detail": "restarting failed container"}


def events(client, service_id=None):  # noqa: F811
    query = f"?service_id={service_id}" if service_id is not None else ""
    return client.get("/api/events" + query, headers=auth("read")).json()["events"]


def test_kubernetes_events_are_recorded_once_per_occurrence(client):  # noqa: F811
    assert post(client, snapshot(0, [instance()], events=[kube_event()])).json()["accepted"]
    # Relisted after a reconnect: same uid, count and time -> nothing new.
    post(client, snapshot(30, [instance()], events=[kube_event()]))
    post(client, snapshot(60, [instance()], events=[kube_event(count=4, last=120), kube_event(uid="e2", reason="Killing")]))
    recorded = [e for e in events(client, "api") if e["type"] == "runtime-event"]
    assert [(e["uid"], e["count"], e["occurrences"]) for e in recorded] == [("e2", 1, 1), ("e1", 4, 3), ("e1", 1, 1)]
    assert datetime.fromisoformat(recorded[1]["occurred_at"]) == T0 + timedelta(seconds=120)
    assert recorded[1]["actor_source"] == "Kubernetes Event"
    assert recorded[1]["instance_id"] == "p1:app" and "message" not in recorded[1]
    # Events are history, not part of the source's current state.
    source = client.get("/api/overview", headers=auth("read")).json()["sources"][0]
    assert "events" not in source


def test_event_reason_outside_allowlist_is_rejected(client):  # noqa: F811
    assert post(client, snapshot(0, events=[kube_event(reason="Pulled")])).status_code == 422
    assert post(client, snapshot(0, events=[{**kube_event(), "message": "secret"}])).status_code == 422


def test_oom_time_survives_docker_restart_and_is_exported(client):  # noqa: F811
    post(client, snapshot(0, [instance()]))
    post(client, snapshot(30, [instance(last_oom_at=at(20))]))
    # Agent restarted (watch state lost) and the container was restarted by its policy: no OOM reported.
    post(client, snapshot(60, [instance(restarts=1)]))
    current = client.get("/api/overview", headers=auth("read")).json()["sources"][0]["instances"][0]
    assert datetime.fromisoformat(current["last_oom_at"]) == T0 + timedelta(seconds=20)
    restarted, oom = events(client, "api")  # source-connected has no service
    assert (restarted["type"], list(restarted["changes"])) == ("instance-restarted", ["restarts"])
    assert (oom["type"], list(oom["changes"])) == ("instance-oom", ["last_oom_at"])
    metrics = client.get("/metrics").text
    line = next(x for x in metrics.splitlines() if x.startswith("gman_instance_last_oom_timestamp_seconds{"))
    assert 'instance_id="c1"' in line and 'reason="OOMKilled"' in line
    assert float(line.rsplit(" ", 1)[1]) == (T0 + timedelta(seconds=20)).timestamp()


def test_silent_source_leaves_a_gap_per_service(client):  # noqa: F811
    instances = [instance("c1", "api"), instance("c2", "web"), instance("c3", "")]
    post(client, snapshot(0, instances))
    post(client, snapshot(60, instances))
    assert not [e for e in events(client) if e["type"] == "history-gap"]
    post(client, snapshot(60 + 301, instances[:2]))
    gaps = [e for e in events(client) if e["type"] == "history-gap"]
    assert sorted(e["service_id"] for e in gaps) == ["", "api", "web"]
    assert gaps[0]["reasons"] == ["source-silent"] and gaps[0]["gap_seconds"] == 301
    assert gaps[0]["gap_from"] == datetime.fromisoformat(at(60)).isoformat()
    assert [e["type"] for e in events(client, "web")][0] == "history-gap"


def test_spool_overflow_is_a_gap(client, monkeypatch):  # noqa: F811
    post(client, snapshot(0, [instance()]))
    post(client, snapshot(10, [instance()], errors=["agent spool overflow: dropped 7 oldest unsent snapshot(s)"]))
    [gap] = [e for e in events(client) if e["type"] == "history-gap"]
    assert (gap["reasons"], gap["dropped_snapshots"], gap["service_id"]) == (["spool-overflow"], 7, "api")
    monkeypatch.setenv("GMAN_HISTORY_GAP_SECONDS", "5")
    post(client, snapshot(20, []))
    gaps = [e for e in events(client) if e["type"] == "history-gap"]
    assert len(gaps) == 2 and gaps[0]["reasons"] == ["source-silent"] and gaps[0]["service_id"] == "api"
