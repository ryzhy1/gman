from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app import db as store
from backend.tests.test_api import client, post  # noqa: F401  (fixture)

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def instance(id_, image="registry/api:1.0", image_id=OLD, created=None, **extra):
    return {"id": id_, "name": id_, "runtime": "docker", "status": "running", "service_id": "api",
            "environment": "prod", "created_at": created or ago(hours=2), "started_at": created or ago(hours=2),
            "config": {"image": image, "image_id": image_id}, **extra}


class Agent:
    """Monotonic snapshots for node-1."""

    def __init__(self, client):
        self.client, self.offset = client, -200

    def send(self, *instances):
        self.offset += 1
        value = {"source_id": "node-1", "source_type": "docker-host", "hostname": "host",
                 "observed_at": (datetime.now(timezone.utc) + timedelta(seconds=self.offset)).isoformat(),
                 "instances": list(instances)}
        response = post(self.client, value)
        assert response.status_code == 200, response.text
        return response.json()


def deploy(client, status="started", run="bamboo-deploy-1", **extra):
    value = {"event_id": f"bamboo:{run}:{status}", "deployment_id": run, "service_id": "api",
             "environment": "prod", "status": status, "build_id": "API-PLAN-7", "release": "release-7",
             "actor": "ivanov", "actor_kind": "human", **extra}
    response = client.post("/api/deployments", json=value)
    assert response.status_code == 200, response.text
    return response.json()


def releases(client, **params):
    response = client.get("/api/releases", params={"service_id": "api", **params})
    assert response.status_code == 200, response.text
    return response.json()["releases"]


def detail(client, release_id):
    response = client.get(f"/api/releases/{release_id}")
    assert response.status_code == 200, response.text
    return response.json()


def events(client):
    return client.get("/api/events", params={"service_id": "api"}).json()["events"]


def test_correlation_by_digest_attributes_runtime_changes(client):
    agent = Agent(client)
    agent.send(instance("old-1"), instance("old-2"))
    release_id = deploy(client, image_digest=NEW, image="registry/api:2.0")["release_id"]
    [release] = releases(client)
    assert (release["status"], release["initiator"]) == ("started", "ivanov")
    assert release["progress"] == {"total": 2, "on_target": 0, "healthy": 0, "fresh": 0}

    agent.send(instance("old-1"), instance("new-1", "registry/api:2.0", NEW, ago(seconds=5)))
    [release] = releases(client)
    assert release["status"] == "rolling-out" and release["correlation"] == "digest"
    assert release["progress"] == {"total": 2, "on_target": 1, "healthy": 1, "fresh": 1}

    agent.send(instance("new-1", "registry/api:2.0", NEW, ago(seconds=5)),
               instance("new-2", "registry/api:2.0", NEW, ago(seconds=2)))
    [release] = releases(client)
    assert release["status"] == "succeeded" and release["finished_at"] and release["duration_seconds"] >= 0

    history = {(e["type"], e.get("instance_id", "")): e for e in events(client)}
    appeared = history[("instance-appeared", "new-1")]
    assert (appeared["actor"], appeared["actor_source"], appeared["release_id"]) == \
        ("ivanov", "Bamboo, correlated by digest", release_id)
    removed = history[("instance-removed", "old-2")]
    assert removed["release_id"] == release_id and removed["actor"] == "unknown"

    data = detail(client, release_id)
    assert {i["id"]: i["on_target"] for i in data["instances"]} == {"new-1": "digest", "new-2": "digest"}
    assert {e["type"] for e in data["events"]} == {"deployment", "instance-appeared", "instance-removed"}


def test_correlation_by_version_tag_and_registry_digest_veto(client):
    agent = Agent(client)
    agent.send(instance("old"))
    deploy(client, version="2.1.0")
    agent.send(instance("new", "registry/api:2.1.0", OTHER, ago(seconds=3)))
    [release] = releases(client)
    assert (release["status"], release["correlation"]) == ("succeeded", "version")
    appeared = next(e for e in events(client) if e["type"] == "instance-appeared")
    assert appeared["actor_source"] == "Bamboo, correlated by version"

    # The runtime knows the manifest digest (Kubernetes imageID) and it differs: same tag, other build.
    deploy(client, run="bamboo-deploy-2", version="2.2.0", image_digest=NEW)
    agent.send(instance("other", "registry/api:2.2.0", "registry/api@" + OTHER, ago(seconds=1)))
    release = releases(client)[0]
    assert release["status"] == "started" and release["progress"]["on_target"] == 0


def test_runtime_observed_before_bamboo_event(client):
    agent = Agent(client)
    agent.send(instance("old"))
    agent.send(instance("new", "registry/api:3.0", NEW, ago(seconds=30)))
    assert events(client)[0]["actor"] == "unknown"
    release_id = deploy(client, image_digest=NEW, occurred_at=ago(minutes=1))["release_id"]
    [release] = releases(client)
    assert release["status"] == "succeeded"
    linked = [e for e in events(client) if e.get("release_id") == release_id]
    assert {e["type"] for e in linked} == {"deployment", "instance-appeared", "instance-removed"}
    assert next(e for e in linked if e["type"] == "instance-appeared")["actor"] == "ivanov"
    # The final event joins the same run and does not change a converged release.
    assert deploy(client, "succeeded", image_digest=NEW)["release_id"] == release_id
    [release] = releases(client)
    assert (release["status"], release["bamboo_status"]) == ("succeeded", "succeeded")


def test_bamboo_failure_fails_release(client):
    agent = Agent(client)
    agent.send(instance("old"))
    deploy(client, image_digest=NEW)
    deploy(client, "failed", image_digest=NEW)
    agent.send(instance("new", "registry/api:2.0", NEW, ago(seconds=1)))
    [release] = releases(client)
    assert release["status"] == "failed" and "Bamboo" in release["status_reason"]


def test_timeout_in_periodic_job(client):
    from backend.app import main, releases as module
    agent = Agent(client)
    agent.send(instance("old"))
    deploy(client, image_digest=NEW, occurred_at=ago(minutes=20))
    assert releases(client)[0]["status"] == "started"
    module.expire(main.engine, datetime.now(timezone.utc))
    assert releases(client)[0]["status"] == "started"
    # The housekeeping run after snapshots checks timeouts once a minute.
    assert releases(client, status="started")
    main.retention.run(main.engine, datetime.now(timezone.utc) + timedelta(minutes=11))
    [release] = releases(client)
    assert release["status"] == "failed" and release["status_reason"].startswith("Тайм-аут 30 мин")
    assert "ни один экземпляр" in release["status_reason"]


def test_crash_loop_and_oom_on_new_instances_fail_release(client):
    agent = Agent(client)
    agent.send(instance("old"))
    deploy(client, image_digest=NEW)
    agent.send(instance("old"), instance("new", "registry/api:2.0", NEW, ago(seconds=5), restarts=1))
    assert releases(client)[0]["status"] == "rolling-out"
    agent.send(instance("old"), instance("new", "registry/api:2.0", NEW, ago(seconds=5), restarts=3))
    [release] = releases(client)
    assert release["status"] == "failed" and "new (3 перезапусков)" in release["status_reason"]

    deploy(client, run="bamboo-deploy-2", image_digest=OTHER)
    agent.send(instance("old"), instance("oom", "registry/api:3.0", OTHER, ago(seconds=1),
                                         status="exited", reason="OOMKilled", exit_code=137))
    release = releases(client)[0]
    assert release["status"] == "failed" and "OOMKilled" in release["status_reason"]


def test_superseding_including_late_older_event(client):
    agent = Agent(client)
    agent.send(instance("old"))
    first = deploy(client, image_digest=NEW)["release_id"]
    second = deploy(client, run="bamboo-deploy-2", image_digest=OTHER)["release_id"]
    by_id = {r["id"]: r for r in releases(client)}
    assert by_id[first]["status"] == "superseded" and by_id[first]["superseded_by"] == second
    assert by_id[second]["status"] == "started"
    # An older deployment reported late does not replace the newer one.
    late = deploy(client, run="bamboo-deploy-0", image_digest=NEW, occurred_at=ago(minutes=5))["release_id"]
    by_id = {r["id"]: r for r in releases(client)}
    assert by_id[late]["status"] == "superseded" and by_id[late]["superseded_by"] == second
    assert by_id[second]["status"] == "started"


def test_replays_are_idempotent(client):
    agent = Agent(client)
    agent.send(instance("old"))
    first = deploy(client, image_digest=NEW)
    assert deploy(client, image_digest=NEW) == {"accepted": False, "reason": "duplicate"}
    snapshot = {"source_id": "node-1", "source_type": "docker-host", "hostname": "host",
                "observed_at": ago(seconds=1), "instances": [instance("new", "registry/api:2.0", NEW, ago(seconds=2))]}
    assert post(client, snapshot).json()["accepted"]
    assert not post(client, snapshot).json()["accepted"]
    [release] = releases(client)
    assert release["id"] == first["release_id"] and release["status"] == "succeeded"
    before = detail(client, release["id"])
    from backend.app import main, releases as module
    module.expire(main.engine, datetime.now(timezone.utc) + timedelta(hours=2))
    assert detail(client, release["id"]) == before
    # Without a deployment ID, a new run of the same build/release is a redeploy, not a replay.
    legacy = {"service_id": "api", "environment": "prod", "build_id": "API-PLAN-7", "release": "release-7"}
    for event_id, status in (("a:started", "started"), ("a:ok", "succeeded"), ("b:started", "started")):
        client.post("/api/deployments", json={**legacy, "event_id": event_id, "status": status})
    assert len(releases(client)) == 3


def test_stored_request_without_new_fields_is_still_a_duplicate(client):
    value = {"event_id": "legacy:1", "service_id": "api", "environment": "prod", "status": "started",
             "build_id": "B-1"}
    deploy_response = client.post("/api/deployments", json=value)
    assert deploy_response.json()["accepted"]
    from backend.app import main
    with Session(main.engine) as session:
        row = session.get(store.Deployment, "legacy:1")
        row.request = {k: v for k, v in row.request.items() if k not in ("image", "version", "deployment_id")}
        session.commit()
    assert client.post("/api/deployments", json=value).json()["reason"] == "duplicate"


def test_manual_change_without_release_stays_unknown(client):
    agent = Agent(client)
    agent.send(instance("old"))
    agent.send(instance("manual", "registry/api:hotfix", OTHER, ago(seconds=1)))
    for event in events(client):
        assert event["actor"] == "unknown" and "release_id" not in event


def test_reader_access_and_paging(client):
    for i in range(3):
        deploy(client, run=f"bamboo-deploy-{i}", occurred_at=ago(minutes=10 - i))
    assert client.get("/api/releases/999").status_code == 404
    assert client.get("/api/releases", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/releases", params={"before": "garbage"}).status_code == 422
    page = client.get("/api/releases", params={"limit": 2, "environment": "prod"}).json()
    assert [r["release"] for r in page["releases"]] == ["release-7", "release-7"] and page["next"]
    rest = client.get("/api/releases", params={"limit": 2, "before": page["next"]}).json()
    assert len(rest["releases"]) == 1 and rest["next"] is None
    ids = [r["id"] for r in page["releases"] + rest["releases"]]
    assert ids == sorted(ids, reverse=True)
    assert releases(client, environment="stage") == []


def test_retention_keeps_open_releases(client, monkeypatch):
    from backend.app import main
    monkeypatch.setenv("GMAN_RETENTION_DAYS", "30")
    deploy(client)
    deploy(client, run="bamboo-deploy-2")
    old = datetime.now(timezone.utc) - timedelta(days=31)
    with Session(main.engine) as session:
        session.execute(update(store.Release).values(started_at=old, window_start=old))
        session.execute(update(store.Release).where(store.Release.status == "started")
                        .values(status="rolling-out", started_at=datetime.now(timezone.utc)))
        session.commit()
    assert main.retention.run(main.engine)
    with Session(main.engine) as session:
        assert [r.status for r in session.scalars(select(store.Release))] == ["rolling-out"]


def test_metrics_expose_latest_release_status(client):
    deploy(client, image_digest=NEW)
    deploy(client, "failed", image_digest=NEW)
    deploy(client, run="bamboo-deploy-2", environment="stage")
    deploy(client, run="bamboo-deploy-3", environment="stage")
    text = client.get("/metrics").text
    status = [line for line in text.splitlines() if line.startswith("gman_release_status{")]
    assert status == ['gman_release_status{service_id="api",environment="prod",status="failed"} 1.0',
                      'gman_release_status{service_id="api",environment="stage",status="started"} 1.0']
    assert text.count("gman_release_started_timestamp_seconds{") == 2
