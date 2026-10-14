from dataclasses import asdict

from config_history.services.config_diff import ConfigDiff
from snapshot_factory import post, snapshot


def read(client, path, **params):
    return client.get(path, params=params)


def history(client):
    """container-1: v1 -> v2 -> v1 again; container-0 (worker, staging) keeps one config."""
    first = snapshot(-40)
    first["instances"].append({**first["instances"][0], "id": "container-0", "name": "worker",
                               "environment": "staging"})
    post(client, first)
    upgraded = snapshot(-30)
    upgraded["instances"] = [dict(first["instances"][0], config={
        "image": "api:v2", "ports": ["8080/tcp", "9090/tcp"], "memory_limit_bytes": 1024}), first["instances"][1]]
    post(client, upgraded)
    reverted = snapshot(-20)
    reverted["instances"] = first["instances"]
    post(client, reverted)


def test_service_versions_newest_first_with_labels(client):
    history(client)
    data = read(client, "/api/services/api/configs").json()
    versions = data["versions"]
    assert data["next"] is None
    assert [(v["instance_id"], v["current"]) for v in versions] == [
        ("container-1", True), ("container-1", False), ("container-0", True), ("container-1", False)]
    assert versions[0]["config_hash"] == versions[3]["config_hash"] != versions[1]["config_hash"]
    assert versions[2]["instance_name"] == "worker" and versions[2]["environment"] == "staging"
    assert "config" not in versions[0]
    times = [(v["first_seen_at"], v["id"]) for v in versions]
    assert times == sorted(times, reverse=True)


def test_versions_page_with_cursor(client):
    history(client)
    seen, before = [], ""
    while True:
        page = read(client, "/api/services/api/configs", limit=1, before=before).json()
        seen += [v["id"] for v in page["versions"]]
        if not page["next"]:
            break
        before = page["next"]
    assert seen == [v["id"] for v in read(client, "/api/services/api/configs").json()["versions"]]
    assert read(client, "/api/services/api/configs", before="garbage").status_code == 422


def test_instance_versions(client):
    history(client)
    versions = read(client, "/api/instances/node-1/container-1/configs").json()["versions"]
    assert [v["current"] for v in versions] == [True, False, False]
    assert {v["instance_id"] for v in versions} == {"container-1"}
    assert len(read(client, "/api/instances/node-1/container-0/configs").json()["versions"]) == 1


def test_removed_instance_keeps_history(client):
    history(client)
    gone = snapshot(-10)
    gone["instances"] = []
    post(client, gone)
    versions = read(client, "/api/instances/node-1/container-1/configs").json()["versions"]
    assert len(versions) == 3 and not any(v["current"] for v in versions)


def test_version_and_diff(client):
    history(client)
    newest, upgraded, _, oldest = read(client, "/api/services/api/configs").json()["versions"]
    version = read(client, f"/api/configs/{upgraded['id']}").json()
    assert version["config"]["image"] == "api:v2" and version["instance_id"] == "container-1"

    data = read(client, "/api/configs/diff", **{"from": oldest["id"], "to": upgraded["id"]}).json()
    assert data["from"]["id"] == oldest["id"] and data["to"]["id"] == upgraded["id"]
    assert not data["identical"]
    assert "config" not in data["from"]
    assert [(c["path"], c["op"], c["before"], c["after"]) for c in data["changes"]] == [
        ("image", "changed", "api:v1", "api:v2"), ("memory_limit_bytes", "changed", None, 1024),
        ("ports[]", "added", None, "8080/tcp"), ("ports[]", "added", None, "9090/tcp")]
    assert data["summary"] == {"added": 2, "removed": 0, "changed": 2}

    back = read(client, "/api/configs/diff", **{"from": upgraded["id"], "to": newest["id"]}).json()
    assert back["summary"] == {"added": 0, "removed": 2, "changed": 2}
    same = read(client, "/api/configs/diff", **{"from": oldest["id"], "to": newest["id"]}).json()
    assert same["identical"] and same["changes"] == []


def test_launch_parameters_are_versioned_only_once_reported(client):
    # An agent before 0.8 does not report command/args: no new version after the API upgrade.
    post(client, snapshot(-40))
    old = snapshot(-30)
    old["instances"][0]["config"] = {"image": "api:v1", "command": None, "args": None}
    post(client, old)
    versions = read(client, "/api/services/api/configs").json()["versions"]
    assert len(versions) == 1
    assert "command" not in read(client, f"/api/configs/{versions[0]['id']}").json()["config"]
    # The upgraded agent reports them: one new version whose diff shows the added parameters.
    new = snapshot(-20)
    new["instances"][0]["config"] = {"image": "api:v1", "command": ["vllm", "serve"], "args": ["--model", "llama"]}
    post(client, new)
    newest, oldest = read(client, "/api/services/api/configs").json()["versions"]
    data = read(client, "/api/configs/diff", **{"from": oldest["id"], "to": newest["id"]}).json()
    assert [(c["path"], c["op"], c["after"]) for c in data["changes"]] == [
        ("args", "added", ["--model", "llama"]), ("command", "added", ["vllm", "serve"])]


def test_reads_and_not_found(client):
    history(client)
    for path in ("/api/services/api/configs", "/api/instances/node-1/container-1/configs", "/api/configs/1",
                 "/api/configs/diff?from=1&to=2"):
        assert client.get(path).status_code == 200
    assert read(client, "/api/services/missing/configs").status_code == 404
    assert read(client, "/api/instances/node-1/missing/configs").status_code == 404
    assert read(client, "/api/instances/node-2/container-1/configs").status_code == 404
    assert read(client, "/api/configs/999").status_code == 404
    assert read(client, "/api/configs/diff", **{"from": 1, "to": 999}).status_code == 404
    assert read(client, "/api/configs/diff", **{"from": 1}).status_code == 422
    assert read(client, "/api/configs/x").status_code == 422
    assert read(client, f"/api/configs/{2 ** 64}").status_code == 422


def test_catalog_service_without_versions_is_empty_not_404(client):
    client.put("/api/services/new", json={"id": "new", "name": "New"})
    assert read(client, "/api/services/new/configs").json() == {"versions": [], "next": None}


def test_diff_structure():
    before = {"image": "a", "ports": ["80", "80", "443"], "caps": ["A", "B"], "nested": {"x": 1, "y": [1]},
              "old": True}
    after = {"image": "a", "ports": ["80", "8443"], "caps": ["B", "A"], "nested": {"x": 2, "y": [1], "z": None},
             "new": ""}
    changes = ConfigDiff().diff(before, after)
    assert [asdict(change) for change in changes] == [
        {"path": "caps", "op": "changed", "before": ["A", "B"], "after": ["B", "A"]},
        {"path": "nested.x", "op": "changed", "before": 1, "after": 2},
        {"path": "nested.z", "op": "added", "before": None, "after": None},
        {"path": "new", "op": "added", "before": None, "after": ""},
        {"path": "old", "op": "removed", "before": True, "after": None},
        {"path": "ports[]", "op": "removed", "before": "80", "after": None},
        {"path": "ports[]", "op": "removed", "before": "443", "after": None},
        {"path": "ports[]", "op": "added", "before": None, "after": "8443"},
    ]
    assert ConfigDiff().summary(changes) == {"added": 3, "removed": 3, "changed": 2}
    assert ConfigDiff().diff(before, before) == []
