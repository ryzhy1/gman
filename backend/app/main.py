"""Inventory API. Humans sign in via OIDC (auth.py) or shared Bearer tokens; agents and CI have their own tokens."""
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import alerts
from . import auth
from . import configs
from . import db as store
from . import exposition
from . import k8s_tokens
from . import metrics
from . import releases
from . import splunk
from . import workloads

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


def token_principal(authorization):
    """Shared Bearer tokens for API automation (and for people while SSO is not configured)."""
    if matches(authorization, os.getenv("GMAN_ADMIN_TOKEN")):
        return auth.Principal(kind="token", role="admin", subject="GMAN_ADMIN_TOKEN")
    if matches(authorization, os.getenv("GMAN_READ_TOKEN")):
        return auth.Principal(kind="token", role="viewer", subject="GMAN_READ_TOKEN")
    return None


def principal(request: Request, authorization: str = Header(default=""), session: Session = Depends(db)):
    """An explicit Authorization header is authoritative; otherwise the SSO session cookie is used."""
    if authorization:
        found = token_principal(authorization)
    else:
        found = auth.from_session(session, request.cookies.get(auth.session_cookie())) if auth.enabled() else None
    if found is None:
        raise HTTPException(401, "Invalid credential")
    return found


def reader(user: auth.Principal = Depends(principal)):
    return user


def editor(user: auth.Principal = Depends(principal), x_csrf_token: str = Header(default="")):
    """Mutating requests: CSRF token for cookie sessions, then at least the service owner role.
    A read token stays a 401 for writes: tokens are scoped credentials, not identities."""
    if user.kind == "token":
        if user.role != "admin":
            raise HTTPException(401, "Invalid credential")
        return user
    if not auth.csrf_ok(user, x_csrf_token):
        raise HTTPException(403, "CSRF token missing or invalid")
    if not user.at_least("owner"):
        raise HTTPException(403, "Read-only role")
    return user


def ci(authorization: str = Header(default="")):
    authenticate(authorization, os.getenv("GMAN_CI_TOKEN"))


def agent(authorization: str = Header(default="")):
    """Resolve the agent's source_id from its token before the body is validated."""
    tokens = json.loads(os.getenv("GMAN_AGENT_TOKENS", "{}"))
    found = [source for source, token in tokens.items() if matches(authorization, token)]
    if found:
        return found[0]
    # GPU DaemonSet pods: a Kubernetes bound service account token, verified by the cluster (TokenReview).
    identity = k8s_tokens.authenticate(authorization) if k8s_tokens.enabled() else None
    if identity is None:
        raise HTTPException(401, "Invalid credential")
    return identity


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


class SplunkLogs(Strict):
    """Per-service override of the configured index mapping and search template (see app/splunk.py)."""
    index: str | dict[str, str] = ""
    query: str = Field(default="", max_length=2000)

    @field_validator("index")
    @classmethod
    def index_names(cls, value):
        names = [value] if isinstance(value, str) else list(value.values())
        if any(name and not re.match(splunk.INDEX_NAME, name) for name in names):
            raise ValueError("Invalid index name")
        return value

    @field_validator("query")
    @classmethod
    def template(cls, value):
        return splunk.check_template(value) if value else value


class Logs(Strict):
    splunk: SplunkLogs | None = None


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
    logs: Logs | None = None
    # Who may edit this entry with the service owner role: IdP subject, email, username or "group:<name>".
    # Kept in services.extra (no migration); only platform admins change it.
    owners: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=50)

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
    # Latest OOM kill: Docker `oom` event (--watch) or OOMKilled state; Kubernetes last/current termination.
    # Kept by the API once observed, since Docker clears OOMKilled when the container restarts.
    last_oom_at: datetime | None = None
    origin: Origin = Field(default_factory=Origin)
    config: Config


class GPUProcess(Strict):
    """A GPU process and its owner: Docker by container_id; Kubernetes pods also by pod and instance_id."""
    pid: int = Field(ge=0)
    container_id: str = Field(default="", max_length=100)
    used_memory_bytes: int | None = Field(default=None, ge=0)
    pod_uid: str = Field(default="", max_length=100)
    namespace: str = Field(default="", max_length=300)
    pod: str = Field(default="", max_length=300)
    container: str = Field(default="", max_length=300)
    # Kubernetes inventory instance ID "<pod uid>:<container name>"; empty when not attributed to a container.
    instance_id: str = Field(default="", max_length=700)


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


# Same allowlist as agent/collect.py KUBE_EVENT_REASONS; anything else is rejected rather than stored.
KUBE_EVENT_REASONS = {"OOMKilling", "BackOff", "FailedScheduling", "Unhealthy", "Killing", "Failed", "Evicted",
                      "FailedMount", "FailedCreatePodSandBox", "Preempting"}


class RuntimeEvent(Strict):
    """A Kubernetes Event of a pod, without its free-text message (see agent/collect.py kube_event)."""
    uid: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")
    reason: str = Field(max_length=100)
    type: Literal["Normal", "Warning"] = "Warning"
    count: int = Field(default=1, ge=1)
    first_at: datetime | None = None
    last_at: datetime
    service_id: str = Field(default="", max_length=100)
    environment: str = Field(default="unknown", max_length=100)
    namespace: str = Field(default="", max_length=300)
    pod: str = Field(default="", max_length=300)
    container: str = Field(default="", max_length=300)
    instance_id: str = Field(default="", max_length=700)
    instance_name: str = Field(default="", max_length=700)
    component: str = Field(default="", max_length=100)
    detail: str = Field(default="", max_length=300)

    _time = field_validator("last_at")(not_future)

    @field_validator("reason")
    @classmethod
    def allowlisted(cls, value):
        if value not in KUBE_EVENT_REASONS:
            raise ValueError("Event reason is not allowlisted")
        return value


class Snapshot(Strict):
    source_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    observed_at: datetime
    # kubernetes-node: node-local GPU snapshot of one cluster node; pods stay with the cluster source.
    source_type: Literal["docker-host", "kubernetes-cluster", "kubernetes-node"]
    # kubernetes-node only: source_id of the cluster source the node belongs to.
    cluster: str = Field(default="", pattern=r"^[a-zA-Z0-9_.-]{0,100}$")
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
    # kubernetes-cluster: pod Events since the agent's previous report; the API dedupes repeats.
    events: list[RuntimeEvent] = Field(default_factory=list, max_length=1000)

    _time = field_validator("observed_at")(not_future)

    @field_validator("errors")
    @classmethod
    def short_errors(cls, value):
        return [e[:500] for e in value]

    @model_validator(mode="after")
    def node_snapshot(self):
        # Instances of a node are reported by the cluster source; reporting them twice would duplicate history.
        if self.source_type == "kubernetes-node" and (self.instances or not self.cluster):
            raise ValueError("kubernetes-node snapshots carry a cluster and no instances")
        # The portal places node GPUs by (cluster, hostname) and alerts label them by hostname: a node token
        # may only describe the node its source ID names, never another node or cluster.
        if self.source_type == "kubernetes-node" and self.source_id != f"{self.cluster}.{self.hostname}":
            raise ValueError("kubernetes-node source_id must be <cluster>.<node>, the node being the hostname")
        return self


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
    # Image reference and artifact version the deployment rolls out; used to correlate with runtime.
    image: str = Field(default="", max_length=1000)
    version: str = Field(default="", max_length=300)
    release: str = Field(default="", max_length=300)
    # One Bamboo deployment run; started/succeeded/failed of the same run share it.
    deployment_id: str = Field(default="", pattern=r"^[a-zA-Z0-9_.:-]{0,200}$")
    build_id: str = Field(max_length=300)
    build_url: str = Field(default="", max_length=2000)

    _url = field_validator("build_url")(http_url)

    @field_validator("occurred_at")
    @classmethod
    def timestamp(cls, value):
        return value if value is None else not_future(value)


# Changes to these fields are runtime state, not configuration.
STATE_FIELDS = {"status", "health", "ready", "reason", "exit_code", "last_termination_reason", "finished_at",
                "last_oom_at"}
RESTART_FIELDS = {"restarts", "started_at"}
SPOOL_OVERFLOW = re.compile(r"^agent spool overflow: dropped (\d+)")


def gap_seconds():
    """A source silent for longer than this (5 agent intervals, as GManSourceSilent) leaves a gap in history."""
    return float(os.getenv("GMAN_HISTORY_GAP_SECONDS", "300"))


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
    if "last_oom_at" in names and fields["last_oom_at"][1]:
        return "instance-oom"
    if names & RESTART_FIELDS:
        return "instance-restarted"
    return "state-changed"


@asynccontextmanager
async def lifespan(app):
    # Migrating here rather than in the container command keeps one code path for Docker,
    # local runs and tests; concurrent replicas are serialized by a PostgreSQL advisory lock.
    auth.check_config()
    auth.reset()
    k8s_tokens.config()  # fail at startup on a malformed GMAN_K8S_TOKEN_REVIEW
    k8s_tokens.reset()
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


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(session: Session = Depends(db)):
    """Inventory-derived metrics for alert rules. Internal: nginx does not proxy this path."""
    return PlainTextResponse(exposition.render(session), media_type="text/plain; version=0.0.4")


def set_cookie(response, name, value, max_age, path="/"):
    response.set_cookie(name, value, max_age=max_age, path=path, httponly=True, secure=auth.secure(), samesite="lax")


def drop_cookie(response, name, path="/"):
    response.delete_cookie(name, path=path, secure=auth.secure(), httponly=True, samesite="lax")


@app.get("/api/auth/info")
def auth_info():
    """Public: tells the portal whether to redirect to SSO or to ask for a token."""
    return {"oidc": auth.enabled()}


@app.get("/api/auth/login")
def login(next: str = Query("/", max_length=2000)):
    if not auth.enabled():
        raise HTTPException(404, "SSO is not configured")
    try:
        url, state = auth.start_login(next)
    except auth.Failure as error:
        logger.warning("SSO login unavailable: %s", error)
        raise HTTPException(503, "Identity provider unavailable")
    response = RedirectResponse(url, status_code=302)
    set_cookie(response, auth.LOGIN_COOKIE, state, auth.LOGIN_TTL, path="/api/auth")
    return response


@app.get("/api/auth/callback")
def callback(request: Request, code: str = Query("", max_length=4000), state: str = Query("", max_length=500),
             error: str = Query("", max_length=200), session: Session = Depends(db)):
    if not auth.enabled():
        raise HTTPException(404, "SSO is not configured")
    cookie = request.cookies.get(auth.LOGIN_COOKIE)
    try:
        if error:
            # Without a matching state the error may come from a forged link; treat it as a bad state.
            pending = auth.unsign(cookie or "") or {}
            if not state or pending.get("state") != state:
                raise auth.Failure("state", "IdP error without valid state")
            raise auth.Failure("idp_error", "IdP returned error " + error)
        value, target = auth.complete_login(session, code, state, cookie)
    except auth.Failure as failure:
        logger.warning("SSO sign-in failed: %s", failure)
        if failure.code == "state":
            raise HTTPException(400, "Login state missing, expired or mismatched; start the sign-in again")
        response = RedirectResponse("/?auth_error=" + failure.code, status_code=302)
        drop_cookie(response, auth.LOGIN_COOKIE, path="/api/auth")
        return response
    response = RedirectResponse(target, status_code=302)
    drop_cookie(response, auth.LOGIN_COOKIE, path="/api/auth")
    set_cookie(response, auth.session_cookie(), value, int(auth.session_hours() * 3600))
    return response


@app.post("/api/auth/logout")
def logout(user: auth.Principal = Depends(principal), x_csrf_token: str = Header(default=""),
           session: Session = Depends(db)):
    if user.kind != "session":
        raise HTTPException(400, "Bearer tokens have no session to end")
    if not auth.csrf_ok(user, x_csrf_token):
        raise HTTPException(403, "CSRF token missing or invalid")
    auth.end_session(session, user)
    response = JSONResponse({"redirect": auth.logout_url()})
    drop_cookie(response, auth.session_cookie())
    return response


@app.get("/api/me")
def me(user: auth.Principal = Depends(reader)):
    """Who is signed in and what the portal may offer them; identities are matched against service owners."""
    return {"auth": "oidc" if user.kind == "session" else "token", "role": user.role, "subject": user.subject,
            "email": user.email, "name": user.name or user.username or user.email or user.subject,
            "identities": user.identities() if user.kind == "session" else [],
            "csrf_token": user.csrf_token or None}


@app.put("/api/services/{service_id}")
def put_service(service_id: str, value: Service, user: auth.Principal = Depends(editor),
                session: Session = Depends(db)):
    """Platform admins create and edit any entry; service owners edit entries listing them, except `owners`."""
    if service_id != value.id:
        raise HTTPException(422, "ID mismatch")
    lock(session, "service:" + value.id)
    before = store.service(session, value.id)
    # Optional nested sections (logs) are omitted when unset rather than stored as null.
    after = value.model_dump(exclude_none=True)
    if user.role != "admin":
        if before is None:
            raise HTTPException(403, "Only platform admins register new services")
        if not user.can_edit(before):
            raise HTTPException(403, "You are not an owner of this service")
        if sorted(before.get("owners") or []) != sorted(value.owners):
            raise HTTPException(403, "Only platform admins change service owners")
    store.put_service(session, after)
    diff = changes(before or {}, after)
    if before is None or diff:
        received = datetime.now(timezone.utc)
        human = user.kind == "session"
        # A shared admin token may be used by a person or a script: its actor is not a known human.
        session.add(store.event({
            "type": "service-created" if before is None else "service-updated", "service_id": value.id,
            "actor": user.actor, "actor_kind": "human" if human else "unknown",
            "actor_source": "OIDC session" if human else "shared admin token",
            "actor_subject": user.subject, "actor_role": user.role,
            "occurred_at": received.isoformat(), "received_at": received.isoformat(), "changes": diff},
            received, received))
    session.commit()
    return after


@app.get("/api/overview", dependencies=[Depends(reader)])
def overview(session: Session = Depends(db)):
    return {"services": store.services(session), "sources": store.sources(session), "server_time": now(),
            # Browser-facing base URL of the bundled Grafana, for default dashboard links.
            "grafana_url": os.getenv("GMAN_GRAFANA_URL", "").rstrip("/"),
            "events": store.events(session, 200)["events"]}


@app.get("/api/logs/splunk", dependencies=[Depends(reader)])
def splunk_links(service_id: str = Query(max_length=100), environment: str = Query("", max_length=200),
                 range_: Literal["1h", "24h", "7d"] = Query("1h", alias="range"), session: Session = Depends(db)):
    """Splunk search links for a service and its instances; built here so values are SPL-escaped."""
    service = next((s for s in store.services(session) if s["id"] == service_id), None)
    if service is None:
        raise HTTPException(404, "Unknown service")
    try:
        return {"error": None, **splunk.links(service, store.sources(session), environment, range_)}
    except splunk.ConfigError as error:
        logger.warning("Splunk links disabled: %s", error)
        return {"enabled": False, "error": "Splunk links are misconfigured", "service": None, "instances": {}}


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


@app.get("/api/workloads", dependencies=[Depends(reader)])
def workload_state(service_id: str | None = Query(None, max_length=300),
                   environment: str | None = Query(None, max_length=300),
                   cluster: str | None = Query(None, max_length=300)):
    """Kubernetes workloads of a service (optionally one environment) or of a cluster, from kube-state-metrics.
    Needs service_id or cluster; an unavailable store is reported like /api/metrics/current."""
    if not service_id and not cluster:
        raise HTTPException(422, "service_id or cluster is required")
    if not metrics.base_url():
        return {"enabled": False, "error": "PROMETHEUS_URL is not configured", "workloads": []}
    matchers = {"service_id": service_id, "environment": environment, "source_id": cluster}
    try:
        return {"enabled": True, "error": None,
                "workloads": workloads.workloads({k: v for k, v in matchers.items() if v})}
    except KeyError:
        raise HTTPException(422, "Invalid service, environment or cluster")
    except metrics.Unavailable as error:
        return {"enabled": True, "error": "Metrics store unavailable: " + str(error), "workloads": []}


@app.get("/api/alerts", dependencies=[Depends(reader)])
def active_alerts():
    """Firing alerts from Alertmanager, without silenced and inhibited ones."""
    if not alerts.base_url():
        return {"enabled": False, "error": "ALERTMANAGER_URL is not configured", "alerts": []}
    try:
        return {"enabled": True, "error": None, "alerts": alerts.active()}
    except alerts.Unavailable as error:
        return {"enabled": True, "error": "Alertmanager unavailable: " + str(error), "alerts": []}


@app.get("/api/metrics/range", dependencies=[Depends(reader)])
def metrics_range(scope: Literal["host", "container", "gpu", "k8s_container", "k8s_pod", "k8s_gpu"],
                  metric: str = Query(max_length=100), range_: str = Query("1h", alias="range", max_length=10),
                  host_id: str | None = Query(None, max_length=300),
                  container_id: str | None = Query(None, max_length=300), uuid: str | None = Query(None, max_length=300),
                  pod_uid: str | None = Query(None, max_length=300),
                  container_name: str | None = Query(None, max_length=300),
                  namespace: str | None = Query(None, max_length=300), pod_name: str | None = Query(None, max_length=300)):
    """Object parameters by scope: host: host_id; container: host_id, container_id; gpu: host_id, uuid;
    k8s_container: pod_uid, container_name; k8s_pod: pod_uid; k8s_gpu: namespace, pod_name, container_name.
    Any other combination is a 422."""
    if not metrics.base_url():
        raise HTTPException(503, "PROMETHEUS_URL is not configured")
    # DCGM labels pods by name, kubelet_stats by UID: the same container_name parameter maps to either label.
    container_label = "container" if scope == "k8s_gpu" else "k8s_container_name"
    matchers = {"host_id": host_id, "container_id": container_id, "UUID": uuid, "k8s_pod_uid": pod_uid,
                container_label: container_name, "namespace": namespace, "pod": pod_name}
    try:
        return metrics.series(scope, metric, {k: v for k, v in matchers.items() if v is not None}, range_)
    except KeyError:
        raise HTTPException(422, "Unknown metric, range or object")
    except metrics.Unavailable as error:
        raise HTTPException(503, "Metrics store unavailable: " + str(error))


def history_gap(session, base, since, value, old, new, received):
    """Marks a period whose changes may be missing: the source was silent, or its agent dropped spooled snapshots.

    One event per affected service (and one without a service for unlabeled instances), so that both the
    service history and the audit show it; the portal collapses the copies in the audit.
    """
    reasons, dropped = [], 0
    silent = (value.observed_at - since).total_seconds()
    if silent > gap_seconds():
        reasons.append("source-silent")
    for error in value.errors:
        match = SPOOL_OVERFLOW.match(error)
        if match:
            dropped += int(match[1])
    if dropped:
        reasons.append("spool-overflow")
    if not reasons:
        return
    services = sorted({i["service_id"] for i in [*old.values(), *new.values()]}) or [""]
    for service_id in services:
        session.add(store.event({**base, "type": "history-gap", "service_id": service_id, "reasons": reasons,
                                 "gap_from": since.isoformat(), "gap_to": base["occurred_at"],
                                 "gap_seconds": round(silent), "dropped_snapshots": dropped},
                                received, value.observed_at))


def runtime_events(session, base, events, received):
    """Kubernetes Events as history entries, once per new occurrence (uid + count/last time)."""
    for item in {e["uid"]: e for e in events}.values():
        last_at = datetime.fromisoformat(item["last_at"])
        mark = session.get(store.RuntimeEventMark, (base["source_id"], item["uid"]))
        if mark is not None and mark.count >= item["count"] and mark.last_at >= last_at:
            continue  # already recorded: the agent reports Events again after a relist
        occurrences = item["count"] - mark.count if mark is not None and item["count"] > mark.count else (
            1 if mark is not None else item["count"])
        if mark is None:
            mark = store.RuntimeEventMark(source_id=base["source_id"], uid=item["uid"], count=0, last_at=last_at)
            session.add(mark)
        mark.count, mark.last_at, mark.received_at = max(mark.count, item["count"]), max(mark.last_at, last_at), received
        fields = {key: value for key, value in item.items() if key != "type"}
        session.add(store.event({**base, **fields, "type": "runtime-event", "event_type": item["type"],
                                 "occurred_at": item["last_at"], "occurrences": occurrences,
                                 "actor_source": "Kubernetes Event"}, received, last_at))


@app.post("/api/inventory")
def ingest(value: Snapshot, source_id: str = Depends(agent), session: Session = Depends(db)):
    if value.source_id != source_id:
        raise HTTPException(403, "Token is not valid for this source")
    k8s_tokens.check_snapshot(source_id, value)
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
    since = previous.observed_at if previous else None  # save_snapshot merges the new values into `previous`
    old = store.save_snapshot(session, data, received)
    added = []
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
            added.append(store.event(event, received, value.observed_at))
            session.add(added[-1])
        history_gap(session, base, since, value, old, new, received)
    runtime_events(session, base, data["events"], received)
    # Releases of every service/environment whose instances changed; the first snapshot may finish
    # a rollout as well, since it is the first time these instances are seen.
    touched = {(x["service_id"], x["environment"]) for x in old.values() if new.get(x["id"]) != x}
    touched |= {(x["service_id"], x["environment"]) for x in new.values() if old.get(x["id"]) != x}
    releases.on_snapshot(session, added, touched, received)
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
        # Requests stored before optional fields were added lack them; compare with their defaults.
        defaults = {k: f.default for k, f in Deployment.model_fields.items() if not f.is_required()}
        if {**defaults, **existing.request} != request:
            raise HTTPException(409, "Event ID reused with different payload")
        return {"accepted": False, "reason": "duplicate"}
    received_at = datetime.now(timezone.utc)
    received = received_at.isoformat()
    session.add(store.Deployment(event_id=value.event_id, request=request, received_at=received_at))
    event = {**request, "occurred_at": request["occurred_at"] or received, "type": "deployment",
             "received_at": received, "actor_source": "Bamboo integration assertion"}
    row = store.event(event, received_at, value.occurred_at or received_at)
    session.add(row)
    release = releases.on_deployment(session, request, row, received_at)
    session.commit()
    return {"accepted": True, "release_id": release.id}


@app.get("/api/releases", dependencies=[Depends(reader)])
def list_releases(service_id: str | None = Query(None, max_length=100),
                  environment: str | None = Query(None, max_length=100),
                  status: Literal[releases.STATUSES] | None = None,
                  limit: int = Query(50, ge=1, le=500), before: str = Query("", max_length=200),
                  session: Session = Depends(db)):
    """Newest first by start; pass `next` as `before` for the next page."""
    try:
        return releases.listing(session, limit, service_id, environment, status, before,
                                datetime.now(timezone.utc))
    except ValueError:
        raise HTTPException(422, "Invalid cursor")


@app.get("/api/releases/{release_id}", dependencies=[Depends(reader)])
def release_detail(release_id: int, session: Session = Depends(db)):
    """Release with its correlated events and the current instances of its rollout."""
    result = releases.detail(session, release_id, datetime.now(timezone.utc))
    if result is None:
        raise HTTPException(404, "Release not found")
    return result
