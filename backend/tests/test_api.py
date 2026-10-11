import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.tests.database import database_url


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url(tmp_path))
    for key, value in {"GMAN_READ_TOKEN": "read", "GMAN_ADMIN_TOKEN": "admin",
                       "GMAN_AGENT_TOKENS": '{"node-1":"agent","node-2":"agent2"}', "GMAN_CI_TOKEN": "ci"}.items():
        monkeypatch.setenv(key, value)
    from backend.app import main
    importlib.reload(main)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c
    main.engine.dispose()


def auth(token):
    return {"Authorization": "Bearer " + token}


def snapshot(offset=0):
    return {"source_id": "node-1", "source_type": "docker-host", "hostname": "host",
            "observed_at": (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(),
            "instances": [{"id": "container-1", "name": "api", "runtime": "docker", "status": "running",
                           "service_id": "api", "started_at": "2026-10-10T10:00:00.123456789Z",
                           "config": {"image": "api:v1"}}]}


def post(client, value):
    return client.post("/api/inventory", headers=auth("agent"), json=value)


def test_auth_and_scope(client):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers=auth("read")).status_code == 200
    assert client.put("/api/services/api", headers=auth("read"), json={"id":"api","name":"API"}).status_code == 401
    value = snapshot()
    value["source_id"] = "node-2"
    assert post(client, value).status_code == 403
    assert client.post("/api/deployments", headers=auth("agent"), json={}).status_code == 401


def test_auth_checked_before_body_validation(client):
    response = client.post("/api/inventory", json={"source_id": "node-1", "bogus": 1})
    assert response.status_code == 401
    assert "observed_at" not in response.text


def test_non_ascii_credential_is_rejected_not_crashed(client):
    headers = {"Authorization": "Bearer \xe9".encode("latin-1")}
    assert client.get("/api/overview", headers=headers).status_code == 401


def test_inventory_order_history_and_removal(client):
    first = snapshot(-10)
    assert post(client, first).json()["accepted"]
    assert not post(client, first).json()["accepted"]
    assert not post(client, snapshot(-30)).json()["accepted"]
    updated = snapshot(-5)
    updated["instances"][0]["config"]["image"] = "api:v2"
    post(client, updated)
    removed = snapshot()
    removed["instances"] = []
    post(client, removed)
    data = client.get("/api/overview", headers=auth("read")).json()
    assert data["sources"][0]["instances"] == []
    assert [e["type"] for e in data["events"]] == ["instance-removed", "config-changed", "source-connected"]
    assert data["events"][1]["changes"] == {"config.image": ["api:v1", "api:v2"]}
    assert data["events"][2]["instance_count"] == 1
    assert all(e["actor"] == "unknown" for e in data["events"])


def test_state_and_restart_events_are_classified(client):
    post(client, snapshot(-20))
    unhealthy = snapshot(-10)
    unhealthy["instances"][0]["health"] = "unhealthy"
    post(client, unhealthy)
    restarted = snapshot()
    restarted["instances"][0].update(health="unhealthy", restarts=1, started_at="2026-10-10T11:00:00Z")
    post(client, restarted)
    events = client.get("/api/events", headers=auth("read"), params={"service_id": "api"}).json()["events"]
    assert [e["type"] for e in events] == ["instance-restarted", "state-changed"]


def test_service_history_is_not_limited_by_global_window(client):
    post(client, snapshot(-30))
    for i in range(3):
        value = {"event_id": f"other:{i}", "service_id": "other", "environment": "prod",
                 "status": "succeeded", "build_id": f"B-{i}"}
        client.post("/api/deployments", headers=auth("ci"), json=value)
    first = client.get("/api/events", headers=auth("read"), params={"limit": 2}).json()
    assert len(first["events"]) == 2 and first["next"]
    rest = client.get("/api/events", headers=auth("read"), params={"limit": 2, "before": first["next"]}).json()
    assert len(rest["events"]) == 2 and rest["next"] is None
    own = client.get("/api/events", headers=auth("read"), params={"service_id": "other"}).json()["events"]
    assert {e["build_id"] for e in own} == {"B-0", "B-1", "B-2"}


def test_reject_secrets_and_unsafe_urls(client):
    value = snapshot()
    value["instances"][0]["config"]["env"] = {"PASSWORD": "secret"}
    assert post(client, value).status_code == 422
    assert client.put("/api/services/api", headers=auth("admin"), json={"id":"api","name":"API","repository":"javascript:alert(1)"}).status_code == 422
    assert client.put("/api/services/api", headers=auth("admin"), json={"id":"api","name":"API","repository":"https://git.example/api"}).status_code == 200


def test_duplicate_deployment_and_unknown_actor(client):
    value = {"event_id":"build:1:done","service_id":"api","environment":"prod",
             "status":"succeeded", "build_id":"PLAN-1"}
    assert client.post("/api/deployments", headers=auth("ci"), json=value).json()["accepted"]
    # A retry without occurred_at is the same event, not a conflict.
    assert not client.post("/api/deployments", headers=auth("ci"), json=value).json()["accepted"]
    value["status"] = "failed"
    assert client.post("/api/deployments", headers=auth("ci"), json=value).status_code == 409
    data = client.get("/api/overview", headers=auth("read")).json()
    assert len(data["events"]) == 1
    assert data["events"][0]["actor"] == "unknown"
    assert data["events"][0]["occurred_at"] == data["events"][0]["received_at"]


def test_timestamps_and_duplicate_instances(client):
    value = snapshot()
    value["observed_at"] = "2026-10-10T00:00:00"
    assert post(client, value).status_code == 422
    value = snapshot()
    value["instances"].append(value["instances"][0])
    assert post(client, value).status_code == 422
