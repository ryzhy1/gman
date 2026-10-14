import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas.deployments import Deployment
from api.schemas.inventory import Snapshot
from conftest import orchestrators
from database.tables import ConfigSnapshotRecord, DeploymentRecord, EventRecord, InstanceRecord
from database.unit_of_work import StorageError
from snapshot_factory import post, snapshot


def rows(engine, model, *order):
    with Session(engine) as session:
        return session.scalars(select(model).order_by(*order)).all()


def test_service_update_round_trips(client):
    value = {"id": "api", "name": "API", "description": "d", "owner": "o", "team": "t", "repository": "",
             "grafana": "", "splunk": "", "bamboo": ""}
    assert client.put("/api/services/api", json=value).status_code == 200
    value["name"] = "API v2"
    assert client.put("/api/services/api", json=value).json() == value
    assert client.get("/api/overview").json()["services"] == [value]


def test_instances_keep_order_and_config_history_is_deduplicated(client, engine):
    first = snapshot(-40)
    first["instances"].append({**first["instances"][0], "id": "container-0", "name": "worker"})
    post(client, first)
    restarted = snapshot(-30)
    restarted["instances"] = [dict(i) for i in first["instances"]]
    restarted["instances"][0]["restarts"] = 1
    post(client, restarted)
    upgraded = snapshot(-20)
    upgraded["instances"] = [dict(i, config={"image": "api:v2"}) if i["id"] == "container-1" else i
                             for i in restarted["instances"]]
    post(client, upgraded)
    reverted = snapshot(-10)
    reverted["instances"] = restarted["instances"]
    post(client, reverted)

    source = client.get("/api/overview").json()["sources"][0]
    assert [i["id"] for i in source["instances"]] == ["container-1", "container-0"]
    assert source["instances"][0]["restarts"] == 1
    history = rows(engine, ConfigSnapshotRecord, ConfigSnapshotRecord.id)
    assert [(h.instance_id, h.config["image"]) for h in history] == [
        ("container-1", "api:v1"), ("container-0", "api:v1"), ("container-1", "api:v2"), ("container-1", "api:v1")]
    # The unchanged config of container-0 is refreshed rather than duplicated.
    assert history[1].last_seen_at > history[1].first_seen_at
    current = {i.instance_id: i.config_snapshot_id for i in rows(engine, InstanceRecord, InstanceRecord.instance_id)}
    assert current == {"container-0": history[1].id, "container-1": history[3].id}


def test_events_cursor_pages_through_everything(client):
    for i in range(5):
        value = {"event_id": f"e:{i}", "service_id": "api", "environment": "prod",
                 "status": "succeeded", "build_id": f"B-{i}"}
        client.post("/api/deployments", json=value)
    seen, before = [], ""
    while True:
        page = client.get("/api/events", params={"limit": 2, "before": before}).json()
        seen += [e["build_id"] for e in page["events"]]
        if not page["next"]:
            break
        before = page["next"]
    assert seen == [f"B-{i}" for i in reversed(range(5))]
    assert client.get("/api/events", params={"before": "garbage"}).status_code == 422


def test_retention_deletes_old_history_at_most_hourly(make_client, engine):
    client = make_client(retention_days=30)
    old = datetime.now(timezone.utc) - timedelta(days=31)
    with Session(engine) as session:
        session.add(EventRecord(type="deployment", service_id="api", data={"type": "deployment", "service_id": "api"},
                                received_at=old, occurred_at=old))
        session.add(ConfigSnapshotRecord(source_id="gone", instance_id="x", service_id="", config_hash="0",
                                         config={}, first_seen_at=old, last_seen_at=old))
        session.commit()
    post(client, snapshot(-10))
    assert [e.type for e in rows(engine, EventRecord, EventRecord.id)] == ["source-connected"]
    assert [c.instance_id for c in rows(engine, ConfigSnapshotRecord, ConfigSnapshotRecord.id)] == ["container-1"]

    # The next purge waits for the interval to pass.
    inventory = orchestrators(client).inventory
    assert not inventory.run_housekeeping(datetime.now(timezone.utc))
    assert inventory.run_housekeeping(datetime.now(timezone.utc) + timedelta(hours=1, seconds=1))


def test_concurrent_writes_are_serialized(client, engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("advisory locks need PostgreSQL (set GMAN_TEST_DATABASE_URL)")
    modules = orchestrators(client)

    def deploy(_):
        value = Deployment(event_id="build:7", service_id="api", environment="prod", status="succeeded",
                           build_id="B-7")
        return modules.releases.record_deployment(value.model_dump(mode="json"))["accepted"]

    def ingest(offset):
        try:
            return modules.inventory.ingest(Snapshot.model_validate(snapshot(-offset)).model_dump(mode="json"))["accepted"]
        except StorageError:
            return None

    with ThreadPoolExecutor(16) as pool:
        assert sorted(pool.map(deploy, range(16))) == [False] * 15 + [True]
        results = list(pool.map(ingest, range(1, 33)))
    assert all(r is not None for r in results) and any(results)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(DeploymentRecord)) == 1
        connected = session.scalar(select(func.count()).select_from(EventRecord)
                                   .where(EventRecord.type == "source-connected"))
        assert connected == 1
        assert session.scalar(select(func.count()).select_from(InstanceRecord)) == 1


def test_models_match_migrations(database_url):
    """`alembic check` fails when the ORM models drift from the migration scripts."""
    backend = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    for command in (["upgrade", "head"], ["check"]):
        result = subprocess.run([sys.executable, "-m", "alembic", *command], cwd=backend, env=env,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
