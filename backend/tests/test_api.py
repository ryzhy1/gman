from snapshot_factory import post, snapshot


def test_no_credentials_needed(client):
    assert client.get("/api/overview").status_code == 200
    assert client.put("/api/services/api", json={"id": "api", "name": "API"}).status_code == 200
    value = snapshot()
    value["source_id"] = "node-2"
    assert post(client, value).json()["accepted"]
    assert client.get("/api/auth/info").status_code == 404


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
    data = client.get("/api/overview").json()
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
    events = client.get("/api/events", params={"service_id": "api"}).json()["events"]
    assert [e["type"] for e in events] == ["instance-restarted", "state-changed"]


def test_service_history_is_not_limited_by_global_window(client):
    post(client, snapshot(-30))
    for i in range(3):
        value = {"event_id": f"other:{i}", "service_id": "other", "environment": "prod",
                 "status": "succeeded", "build_id": f"B-{i}"}
        client.post("/api/deployments", json=value)
    first = client.get("/api/events", params={"limit": 2}).json()
    assert len(first["events"]) == 2 and first["next"]
    rest = client.get("/api/events", params={"limit": 2, "before": first["next"]}).json()
    assert len(rest["events"]) == 2 and rest["next"] is None
    own = client.get("/api/events", params={"service_id": "other"}).json()["events"]
    assert {e["build_id"] for e in own} == {"B-0", "B-1", "B-2"}


def test_env_is_stored_and_unsafe_urls_rejected(client):
    value = snapshot()
    value["instances"][0]["config"]["env"] = {"PASSWORD": "secret"}
    assert post(client, value).status_code == 422  # a list of "NAME=value", not a mapping
    value["instances"][0]["config"]["env"] = ["PASSWORD=secret", "EMPTY="]
    assert post(client, value).json()["accepted"]
    [instance] = client.get("/api/overview").json()["sources"][0]["instances"]
    assert instance["config"]["env"] == ["PASSWORD=secret", "EMPTY="]
    value = snapshot(1)
    value["instances"][0]["config"]["env_files"] = {"/app/.env.local": ["JWT_SECRET=s"]}
    assert post(client, value).json()["accepted"]
    [instance] = client.get("/api/overview").json()["sources"][0]["instances"]
    assert instance["config"]["env_files"] == {"/app/.env.local": ["JWT_SECRET=s"]}
    assert client.put("/api/services/api", json={"id":"api","name":"API","repository":"javascript:alert(1)"}).status_code == 422
    assert client.put("/api/services/api", json={"id":"api","name":"API","repository":"https://git.example/api"}).status_code == 200


def test_duplicate_deployment_and_unknown_actor(client):
    value = {"event_id":"build:1:done","service_id":"api","environment":"prod",
             "status":"succeeded", "build_id":"PLAN-1"}
    assert client.post("/api/deployments", json=value).json()["accepted"]
    # A retry without occurred_at is the same event, not a conflict.
    assert not client.post("/api/deployments", json=value).json()["accepted"]
    value["status"] = "failed"
    assert client.post("/api/deployments", json=value).status_code == 409
    data = client.get("/api/overview").json()
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
