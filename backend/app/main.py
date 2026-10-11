"""Inventory API. Credentials are scoped independently for readers, agents and CI."""
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import configs
from . import db as store
from . import metrics

logger = logging.getLogger("gman")


def now():
    return datetime.now(timezone.utc).isoformat()


engine = store.connect(os.getenv("DATABASE_URL", "sqlite:///./gman.db"))
retention = store.Retention()


def db():
    with Session(engine) as session:
        yield session


def matches(authorization, expected):
    # Compare bytes: compare_digest rejects non-ASCII str, which would surface as a 500.
    return bool(expected) and hmac.compare_digest((authorization or "").encode(), ("Bearer " + expected).encode())


def authenticate(authorization, expected):
    if not matches(authorization, expected):
        raise HTTPException(401, "Invalid credential")


def reader(authorization: str = Header(default="")):
    authenticate(authorization, os.getenv("GMAN_READ_TOKEN"))


def admin(authorization: str = Header(default="")):
    authenticate(authorization, os.getenv("GMAN_ADMIN_TOKEN"))


def ci(authorization: str = Header(default="")):
    authenticate(authorization, os.getenv("GMAN_CI_TOKEN"))


def agent(authorization: str = Header(default="")):
    """Resolve the agent's source_id from its token before the body is validated."""
    tokens = json.loads(os.getenv("GMAN_AGENT_TOKENS", "{}"))
    found = [source for source, token in tokens.items() if matches(authorization, token)]
    if not found:
        raise HTTPException(401, "Invalid credential")
    return found[0]


def lock(session, name):
    """Serialize concurrent writes per key on PostgreSQL. SQLite is for tests only."""
    if engine.dialect.name == "postgresql":
        lock_id = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})


def http_url(value):
    if value:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Use an HTTP(S) URL without credentials")
    return value


def not_future(value):
    if value.tzinfo is None:
        raise ValueError("Timezone required")
    if (value - datetime.now(timezone.utc)).total_seconds() > 300:
        raise ValueError("Timestamp is in the future")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Service(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    owner: str = Field(default="", max_length=200)
    team: str = Field(default="", max_length=200)
    repository: str = Field(default="", max_length=2000)
    grafana: str = Field(default="", max_length=2000)
    splunk: str = Field(default="", max_length=2000)
    bamboo: str = Field(default="", max_length=2000)

    _urls = field_validator("repository", "grafana", "splunk", "bamboo")(http_url)


Strings = list[str]


class Config(Strict):
    image: str = Field(max_length=1000)
    image_id: str = Field(default="", max_length=1000)
    cpu_limit: float | None = Field(default=None, ge=0)
    cpu_request: float | None = Field(default=None, ge=0)
    memory_limit_bytes: int | None = Field(default=None, ge=0)
    memory_request_bytes: int | None = Field(default=None, ge=0)
    cpuset: str = Field(default="", max_length=200)
    restart_policy: str = Field(default="", max_length=200)
    ports: Strings = Field(default_factory=list, max_length=100)
    mounts: Strings = Field(default_factory=list, max_length=200)
    networks: Strings = Field(default_factory=list, max_length=100)
    network_mode: str = Field(default="", max_length=300)
    pid_mode: str = Field(default="", max_length=300)
    ipc_mode: str = Field(default="", max_length=300)
    user: str = Field(default="", max_length=300)
    cap_add: Strings = Field(default_factory=list, max_length=100)
    cap_drop: Strings = Field(default_factory=list, max_length=100)
    shm_size_bytes: int | None = Field(default=None, ge=0)
    ulimits: Strings = Field(default_factory=list, max_length=100)
    log_driver: str = Field(default="", max_length=100)
    probes: Strings = Field(default_factory=list, max_length=10)
    runtime: str = Field(default="", max_length=100)
    gpu_requests: Strings = Field(default_factory=list, max_length=100)
    privileged: bool = False
    readonly_rootfs: bool = False


class Origin(Strict):
    """Allowlisted provenance hints; none of them proves who started a workload."""
    compose_project: str = Field(default="", max_length=2000)
    compose_service: str = Field(default="", max_length=2000)
    compose_working_dir: str = Field(default="", max_length=2000)
    image_source: str = Field(default="", max_length=2000)
    image_revision: str = Field(default="", max_length=2000)
    image_version: str = Field(default="", max_length=2000)
    image_created: str = Field(default="", max_length=2000)
    app_name: str = Field(default="", max_length=2000)
    app_version: str = Field(default="", max_length=2000)
    managed_by: str = Field(default="", max_length=2000)
    helm_chart: str = Field(default="", max_length=2000)
    field_managers: str = Field(default="", max_length=2000)


class Instance(Strict):
    id: str = Field(max_length=300)
    name: str = Field(max_length=300)
    service_id: str = Field(default="", max_length=100)
    environment: str = Field(default="unknown", max_length=100)
    runtime: Literal["docker", "kubernetes"]
    node: str = Field(default="", max_length=300)
    namespace: str = Field(default="", max_length=300)
    controller: str = Field(default="", max_length=600)
    status: str = Field(max_length=100)
    health: str = Field(default="", max_length=100)
    ready: bool | None = None
    reason: str = Field(default="", max_length=500)
    exit_code: int | None = None
    last_termination_reason: str = Field(default="", max_length=500)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    restarts: int = Field(default=0, ge=0)
    origin: Origin = Field(default_factory=Origin)
    config: Config


class GPUProcess(Strict):
    pid: int = Field(ge=0)
    container_id: str = Field(default="", max_length=100)
    used_memory_bytes: int | None = Field(default=None, ge=0)


def percent():
    return Field(default=None, ge=0, le=100)


class GPU(Strict):
    """Unsupported readings are null, never zero; temperatures stay per sensor."""
    id: str = Field(max_length=300)
    vendor: str = Field(max_length=100)
    model: str = Field(default="", max_length=300)
    pci_bus_id: str = Field(default="", max_length=100)
    driver_version: str | None = Field(default=None, max_length=100)
    pstate: str | None = Field(default=None, max_length=10)
    utilization_percent: float | None = percent()
    memory_utilization_percent: float | None = percent()
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_used_bytes: int | None = Field(default=None, ge=0)
    temperature_c: float | None = None
    hotspot_temperature_c: float | None = None
    memory_temperature_c: float | None = None
    power_w: float | None = Field(default=None, ge=0)
    power_limit_w: float | None = Field(default=None, ge=0)
    sm_clock_mhz: float | None = Field(default=None, ge=0)
    memory_clock_mhz: float | None = Field(default=None, ge=0)
    max_sm_clock_mhz: float | None = Field(default=None, ge=0)
    fan_percent: float | None = percent()
    fan_rpm: float | None = Field(default=None, ge=0)
    throttle_reasons: list[str] = Field(default_factory=list, max_length=20)
    ecc_uncorrected_errors: int | None = Field(default=None, ge=0)
    pcie_generation: int | None = Field(default=None, ge=0)
    pcie_width: int | None = Field(default=None, ge=0)
    mig_mode: str | None = Field(default=None, max_length=50)
    persistence_mode: str | None = Field(default=None, max_length=50)
    processes: list[GPUProcess] = Field(default_factory=list, max_length=1000)


class Node(Strict):
    """Kubernetes node with capacity and the requests of pods scheduled on it."""
    name: str = Field(max_length=300)
    ready: bool
    unschedulable: bool = False
    pressure: list[str] = Field(default_factory=list, max_length=20)
    roles: list[str] = Field(default_factory=list, max_length=20)
    taints: list[str] = Field(default_factory=list, max_length=50)
    kubelet_version: str = Field(default="", max_length=100)
    os: str = Field(default="", max_length=300)
    kernel: str = Field(default="", max_length=300)
    runtime_version: str = Field(default="", max_length=300)
    cpu_capacity: float | None = Field(default=None, ge=0)
    cpu_allocatable: float | None = Field(default=None, ge=0)
    cpu_requested: float | None = Field(default=None, ge=0)
    memory_capacity_bytes: int | None = Field(default=None, ge=0)
    memory_allocatable_bytes: int | None = Field(default=None, ge=0)
    memory_requested_bytes: int | None = Field(default=None, ge=0)
    gpu_capacity: int = Field(default=0, ge=0)
    gpu_allocatable: int = Field(default=0, ge=0)
    gpu_requested: int = Field(default=0, ge=0)
    gpu_product: str = Field(default="", max_length=300)
    gpu_memory_mib: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None


class Snapshot(Strict):
    source_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    observed_at: datetime
    source_type: Literal["docker-host", "kubernetes-cluster"]
    agent_version: str = Field(default="", max_length=50)
    hostname: str = Field(max_length=300)
    os: str = Field(default="", max_length=300)
    kernel: str = Field(default="", max_length=300)
    cpu_model: str = Field(default="", max_length=300)
    runtime_version: str = Field(default="", max_length=300)
    uptime_seconds: int | None = Field(default=None, ge=0)
    cpu_count: int | None = Field(default=None, ge=1)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    gpus: list[GPU] = Field(default_factory=list, max_length=128)
    nodes: list[Node] = Field(default_factory=list, max_length=1000)
    instances: list[Instance] = Field(default_factory=list, max_length=10000)
    errors: list[str] = Field(default_factory=list, max_length=50)

    _time = field_validator("observed_at")(not_future)

    @field_validator("errors")
    @classmethod
    def short_errors(cls, value):
        return [e[:500] for e in value]


class Deployment(Strict):
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,200}$")
    service_id: str = Field(max_length=100)
    environment: str = Field(max_length=100)
    # Optional: retries of the same CI step must send an identical payload.
    occurred_at: datetime | None = None
    actor: str = Field(default="unknown", max_length=300)
    actor_kind: Literal["human", "service-account", "schedule", "unknown"] = "unknown"
    status: Literal["started", "succeeded", "failed"]
    commit: str = Field(default="", max_length=200)
    image_digest: str = Field(default="", max_length=1000)
    release: str = Field(default="", max_length=300)
    build_id: str = Field(max_length=300)
    build_url: str = Field(default="", max_length=2000)

    _url = field_validator("build_url")(http_url)

    @field_validator("occurred_at")
    @classmethod
    def timestamp(cls, value):
        return value if value is None else not_future(value)


# Changes to these fields are runtime state, not configuration.
STATE_FIELDS = {"status", "health", "ready", "reason", "exit_code", "last_termination_reason", "finished_at"}
RESTART_FIELDS = {"restarts", "started_at"}


def changes(before, after):
    """Field-level diff; config and origin are flattened one level for readable history."""
    result = {}
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            for sub in old.keys() | new.keys():
                if old.get(sub) != new.get(sub):
                    result[key + "." + sub] = [old.get(sub), new.get(sub)]
        elif old != new:
            result[key] = [old, new]
    return dict(sorted(result.items()))


def classify(fields):
    names = {f.split(".")[0] for f in fields}
    if names - STATE_FIELDS - RESTART_FIELDS:
        return "config-changed"
    if names & RESTART_FIELDS:
        return "instance-restarted"
    return "state-changed"


@asynccontextmanager
async def lifespan(app):
    # Migrating here rather than in the container command keeps one code path for Docker,
    # local runs and tests; concurrent replicas are serialized by a PostgreSQL advisory lock.
    store.migrate(engine)
    yield


app = FastAPI(title="GMan Inventory", version="0.2.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def internal_error(request, error):
    # Do not expose SQL parameters, credentials or inventory in error responses.
    return JSONResponse(status_code=500, content={"detail": "Internal error"})


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.put("/api/services/{service_id}", dependencies=[Depends(admin)])
def put_service(service_id: str, value: Service, session: Session = Depends(db)):
    if service_id != value.id:
        raise HTTPException(422, "ID mismatch")
    lock(session, "service:" + value.id)
    store.put_service(session, value.model_dump())
    session.commit()
    return value


@app.get("/api/overview", dependencies=[Depends(reader)])
def overview(session: Session = Depends(db)):
    return {"services": store.services(session), "sources": store.sources(session), "server_time": now(),
            # Browser-facing base URL of the bundled Grafana, for default dashboard links.
            "grafana_url": os.getenv("GMAN_GRAFANA_URL", "").rstrip("/"),
            "events": store.events(session, 200)["events"]}


@app.get("/api/events", dependencies=[Depends(reader)])
def list_events(limit: int = Query(200, ge=1, le=1000), before: str = Query("", max_length=200),
                service_id: str | None = Query(None, max_length=100), session: Session = Depends(db)):
    """Newest-first audit; pass `next` from a response as `before` for the next page."""
    try:
        return store.events(session, limit, service_id, before)
    except ValueError:
        raise HTTPException(422, "Invalid cursor")


MAX_ID = 2 ** 63 - 1  # BIGINT on PostgreSQL


def config_page(session, limit, before, **scope):
    try:
        return store.config_versions(session, limit, before=before, **scope)
    except ValueError:
        raise HTTPException(422, "Invalid cursor")


@app.get("/api/services/{service_id}/configs", dependencies=[Depends(reader)])
def service_configs(service_id: str, limit: int = Query(100, ge=1, le=1000), before: str = Query("", max_length=200),
                    session: Session = Depends(db)):
    """Config versions of all instances of a service, newest first; without the config bodies."""
    if not store.service_known(session, service_id):
        raise HTTPException(404, "Service not found")
    return config_page(session, limit, before, service_id=service_id)


@app.get("/api/instances/{source_id}/{instance_id}/configs", dependencies=[Depends(reader)])
def instance_configs(source_id: str, instance_id: str, limit: int = Query(100, ge=1, le=1000),
                     before: str = Query("", max_length=200), session: Session = Depends(db)):
    if not store.instance_known(session, source_id, instance_id):
        raise HTTPException(404, "Instance not found")
    return config_page(session, limit, before, instance=(source_id, instance_id))


def config_or_404(session, version_id):
    found = store.config_snapshot(session, version_id)
    if found is None:
        raise HTTPException(404, f"Config version {version_id} not found")
    return found


# Declared before /api/configs/{version_id} so that "diff" is not taken for an ID.
@app.get("/api/configs/diff", dependencies=[Depends(reader)])
def config_diff(from_: int = Query(alias="from", ge=1, le=MAX_ID), to: int = Query(ge=1, le=MAX_ID),
                session: Session = Depends(db)):
    """Changes from version `from` to version `to`; any two versions may be compared."""
    old, new = config_or_404(session, from_), config_or_404(session, to)
    diff = configs.diff(old.pop("config"), new.pop("config"))
    return {"from": old, "to": new, "identical": old["config_hash"] == new["config_hash"],
            "summary": configs.summary(diff), "changes": diff}


@app.get("/api/configs/{version_id}", dependencies=[Depends(reader)])
def config_version(version_id: int = Path(ge=1, le=MAX_ID), session: Session = Depends(db)):
    return config_or_404(session, version_id)


@app.get("/api/metrics/current", dependencies=[Depends(reader)])
def metrics_current():
    """Latest resource usage; absence of data is reported, never turned into zeros."""
    if not metrics.base_url():
        return {"enabled": False, "error": "PROMETHEUS_URL is not configured"}
    try:
        return {"enabled": True, "error": None, **metrics.current()}
    except metrics.Unavailable as error:
        return {"enabled": True, "error": "Metrics store unavailable: " + str(error)}


@app.get("/api/metrics/range", dependencies=[Depends(reader)])
def metrics_range(scope: Literal["host", "container", "gpu", "k8s_container", "k8s_pod"],
                  metric: str = Query(max_length=100), range_: str = Query("1h", alias="range", max_length=10),
                  host_id: str | None = Query(None, max_length=300),
                  container_id: str | None = Query(None, max_length=300), uuid: str | None = Query(None, max_length=300),
                  pod_uid: str | None = Query(None, max_length=300),
                  container_name: str | None = Query(None, max_length=300)):
    """Object parameters by scope: host: host_id; container: host_id, container_id; gpu: host_id, uuid;
    k8s_container: pod_uid, container_name; k8s_pod: pod_uid. Any other combination is a 422."""
    if not metrics.base_url():
        raise HTTPException(503, "PROMETHEUS_URL is not configured")
    matchers = {"host_id": host_id, "container_id": container_id, "UUID": uuid,
                "k8s_pod_uid": pod_uid, "k8s_container_name": container_name}
    try:
        return metrics.series(scope, metric, {k: v for k, v in matchers.items() if v is not None}, range_)
    except KeyError:
        raise HTTPException(422, "Unknown metric, range or object")
    except metrics.Unavailable as error:
        raise HTTPException(503, "Metrics store unavailable: " + str(error))


@app.post("/api/inventory")
def ingest(value: Snapshot, source_id: str = Depends(agent), session: Session = Depends(db)):
    if value.source_id != source_id:
        raise HTTPException(403, "Token is not valid for this source")
    data = value.model_dump(mode="json")
    new = {x["id"]: x for x in data["instances"]}
    if len(new) != len(data["instances"]):
        raise HTTPException(422, "Duplicate instance IDs")
    lock(session, value.source_id)
    previous = session.get(store.Source, value.source_id)
    if previous and previous.observed_at >= value.observed_at:
        return {"accepted": False, "reason": "duplicate or older snapshot"}
    received = datetime.now(timezone.utc)
    data["received_at"] = received.isoformat()
    base = {"source_id": value.source_id, "occurred_at": data["observed_at"], "received_at": data["received_at"],
            "actor": "unknown", "actor_source": "runtime observation"}
    old = store.save_snapshot(session, data, received)
    if previous is None:
        # The first snapshot describes pre-existing workloads; it is neither a deployment nor N changes.
        session.add(store.event({**base, "type": "source-connected", "service_id": "", "instance_count": len(new)},
                                received, value.observed_at))
    else:
        for key in old.keys() | new.keys():
            before, after = old.get(key), new.get(key)
            if before == after:
                continue
            event = {**base, "instance_id": key, "instance_name": (after or before)["name"],
                     "service_id": (after or before)["service_id"]}
            if before is None:
                event.update(type="instance-appeared", after=after)
            elif after is None:
                event.update(type="instance-removed", before=before)
            else:
                diff = changes(before, after)
                event.update(type=classify(diff), changes=diff)
            session.add(store.event(event, received, value.observed_at))
    session.commit()
    try:
        retention.run(engine)
    except Exception:
        # Housekeeping must not turn an already committed snapshot into an error for the agent.
        logger.exception("Retention purge failed")
    return {"accepted": True}


@app.post("/api/deployments", dependencies=[Depends(ci)])
def deployment(value: Deployment, session: Session = Depends(db)):
    request = value.model_dump(mode="json")
    lock(session, "ci:" + value.event_id)
    existing = session.get(store.Deployment, value.event_id)
    if existing:
        if existing.request != request:
            raise HTTPException(409, "Event ID reused with different payload")
        return {"accepted": False, "reason": "duplicate"}
    received_at = datetime.now(timezone.utc)
    received = received_at.isoformat()
    session.add(store.Deployment(event_id=value.event_id, request=request, received_at=received_at))
    event = {**request, "occurred_at": request["occurred_at"] or received, "type": "deployment",
             "received_at": received, "actor_source": "Bamboo integration assertion"}
    session.add(store.event(event, received_at, value.occurred_at or received_at))
    session.commit()
    return {"accepted": True}
