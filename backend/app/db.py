"""PostgreSQL schema and storage helpers. SQLite is supported only for tests and local development.

Snapshots, instances and configs are stored as validated JSON so that fields added to the
API models flow through without a migration; only what is filtered or ordered on is a column.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (JSON, BigInteger, DateTime, ForeignKey, Identity, Index, Integer, String, TypeDecorator,
                        create_engine, delete, select, text, tuple_, update)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Json = JSON().with_variant(JSONB(), "postgresql")
Id = BigInteger().with_variant(Integer(), "sqlite")  # SQLite auto-increments only INTEGER keys


class UTC(TypeDecorator):
    """timestamptz on PostgreSQL; SQLite keeps naive UTC, so the zone is reattached on read."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Service(Base):
    __tablename__ = "services"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String, default="")
    owner: Mapped[str] = mapped_column(String, default="")
    team: Mapped[str] = mapped_column(String, default="")
    repository: Mapped[str] = mapped_column(String, default="")
    grafana: Mapped[str] = mapped_column(String, default="")
    splunk: Mapped[str] = mapped_column(String, default="")
    bamboo: Mapped[str] = mapped_column(String, default="")
    # Fields added to the API model before they get their own column.
    extra: Mapped[dict] = mapped_column(Json, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTC, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTC, default=utcnow, onupdate=utcnow)


SERVICE_FIELDS = ("id", "name", "description", "owner", "team", "repository", "grafana", "splunk", "bamboo")


class Source(Base):
    """Latest accepted snapshot of a Docker host or Kubernetes cluster, without its instances."""
    __tablename__ = "sources"
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_type: Mapped[str] = mapped_column(String)
    hostname: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(UTC)
    received_at: Mapped[datetime] = mapped_column(UTC)
    snapshot: Mapped[dict] = mapped_column(Json)


class ConfigSnapshot(Base):
    """One row per distinct configuration of an instance, for config diffs between versions."""
    __tablename__ = "config_snapshots"
    id: Mapped[int] = mapped_column(Id, Identity(), primary_key=True)
    source_id: Mapped[str] = mapped_column(String)
    instance_id: Mapped[str] = mapped_column(String)
    service_id: Mapped[str] = mapped_column(String, default="")
    # Labels as of the first observation, so the history stays readable after the instance is gone.
    instance_name: Mapped[str] = mapped_column(String, default="", server_default="")
    environment: Mapped[str] = mapped_column(String, default="", server_default="")
    config_hash: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(Json)
    first_seen_at: Mapped[datetime] = mapped_column(UTC)
    last_seen_at: Mapped[datetime] = mapped_column(UTC)

    __table_args__ = (Index("ix_config_snapshots_instance", "source_id", "instance_id", "first_seen_at"),
                      Index("ix_config_snapshots_service", "service_id", "first_seen_at"),
                      Index("ix_config_snapshots_last_seen", "last_seen_at"))


class Instance(Base):
    """Currently observed workloads; replaced by each accepted snapshot."""
    __tablename__ = "instances"
    source_id: Mapped[str] = mapped_column(String, ForeignKey("sources.source_id", ondelete="CASCADE"),
                                           primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column(Integer)  # order in the snapshot, as the agent sent it
    service_id: Mapped[str] = mapped_column(String, default="")
    environment: Mapped[str] = mapped_column(String, default="")
    runtime: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    config_hash: Mapped[str] = mapped_column(String(64))
    config_snapshot_id: Mapped[int | None] = mapped_column(
        Id, ForeignKey("config_snapshots.id", ondelete="SET NULL"), nullable=True)
    data: Mapped[dict] = mapped_column(Json)

    config_snapshot: Mapped[ConfigSnapshot | None] = relationship()

    __table_args__ = (Index("ix_instances_service_id", "service_id"),)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Id, Identity(), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(UTC)
    occurred_at: Mapped[datetime | None] = mapped_column(UTC, nullable=True)
    type: Mapped[str] = mapped_column(String)
    service_id: Mapped[str] = mapped_column(String, default="")
    source_id: Mapped[str] = mapped_column(String, default="")
    instance_id: Mapped[str] = mapped_column(String, default="")
    data: Mapped[dict] = mapped_column(Json)

    __table_args__ = (Index("ix_events_received", received_at.desc(), id.desc()),
                      Index("ix_events_service", "service_id", received_at.desc(), id.desc()))


class Deployment(Base):
    """CI events by ID, for idempotent retries."""
    __tablename__ = "deployments"
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    request: Mapped[dict] = mapped_column(Json)
    received_at: Mapped[datetime] = mapped_column(UTC)


def connect(url):
    return create_engine(url, pool_pre_ping=url.startswith("postgresql"))


def migration_lock(connection):
    """Several API replicas may start together; only one may run DDL at a time."""
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('gman:migrations'))"))


def migrate(engine):
    from alembic import command
    from alembic.config import Config
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
    with engine.begin() as connection:
        migration_lock(connection)
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def services(session):
    return [{**{f: getattr(row, f) for f in SERVICE_FIELDS}, **row.extra}
            for row in session.scalars(select(Service).order_by(Service.id))]


def put_service(session, value):
    known = {k: v for k, v in value.items() if k in SERVICE_FIELDS}
    extra = {k: v for k, v in value.items() if k not in SERVICE_FIELDS}
    row = session.get(Service, value["id"])
    if row is None:
        session.add(Service(**known, extra=extra))
    else:
        for key, item in known.items():
            setattr(row, key, item)
        row.extra = extra


def sources(session):
    instances = {}
    for row in session.scalars(select(Instance).order_by(Instance.source_id, Instance.position)):
        instances.setdefault(row.source_id, []).append(row.data)
    return [{**row.snapshot, "instances": instances.get(row.source_id, []), "received_at": row.received_at.isoformat()}
            for row in session.scalars(select(Source).order_by(Source.source_id))]


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save_snapshot(session, data, received_at):
    """Store an accepted snapshot; returns the previously observed instances by ID."""
    source_id = data["source_id"]
    session.merge(Source(source_id=source_id, source_type=data["source_type"], hostname=data["hostname"],
                         observed_at=datetime.fromisoformat(data["observed_at"]), received_at=received_at,
                         snapshot={k: v for k, v in data.items() if k not in ("instances", "received_at")}))
    rows = {r.instance_id: r for r in session.scalars(select(Instance).where(Instance.source_id == source_id))}
    old = {key: row.data for key, row in rows.items()}
    unchanged = []
    for position, item in enumerate(data["instances"]):
        digest = config_hash(item["config"])
        row = rows.pop(item["id"], None)
        if row is None:
            row = Instance(source_id=source_id, instance_id=item["id"])
            session.add(row)
        if row.config_hash == digest and row.config_snapshot_id is not None:
            unchanged.append(row.config_snapshot_id)
        else:
            row.config_snapshot = ConfigSnapshot(source_id=source_id, instance_id=item["id"],
                                                 service_id=item["service_id"], instance_name=item["name"],
                                                 environment=item["environment"], config_hash=digest,
                                                 config=item["config"], first_seen_at=received_at,
                                                 last_seen_at=received_at)
        row.position, row.config_hash, row.data = position, digest, item
        row.service_id, row.environment = item["service_id"], item["environment"]
        row.runtime, row.status = item["runtime"], item["status"]
    for row in rows.values():
        session.delete(row)
    for start in range(0, len(unchanged), 5000):
        session.execute(update(ConfigSnapshot).where(ConfigSnapshot.id.in_(unchanged[start:start + 5000]))
                        .values(last_seen_at=received_at))
    return old


def event(data, received_at, occurred_at):
    return Event(received_at=received_at, occurred_at=occurred_at, type=data["type"],
                 service_id=data.get("service_id", ""), source_id=data.get("source_id", ""),
                 instance_id=data.get("instance_id", ""), data=data)


def cursor(row, at="received_at"):
    raw = f"{getattr(row, at).isoformat()}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def parse_cursor(value):
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        received, _, row_id = raw.partition("|")
        received_at = datetime.fromisoformat(received)
        if received_at.tzinfo is None:
            raise ValueError
        return received_at, int(row_id)
    except ValueError:
        raise ValueError("Invalid cursor") from None


def ordered(data):
    # JSONB does not keep key order; diffs are presented sorted by field name.
    return {**data, "changes": dict(sorted(data["changes"].items()))} if "changes" in data else data


def events(session, limit, service_id=None, before=None):
    """Newest first by server receive time; `next` is an opaque keyset cursor."""
    query = select(Event)
    if service_id is not None:
        query = query.where(Event.service_id == service_id)
    if before:
        query = query.where(tuple_(Event.received_at, Event.id) < parse_cursor(before))
    rows = session.scalars(query.order_by(Event.received_at.desc(), Event.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    return {"events": [ordered(r.data) for r in page], "next": cursor(page[-1]) if len(rows) > limit else None}


def config_version(row, current):
    return {"id": row.id, "service_id": row.service_id, "source_id": row.source_id, "instance_id": row.instance_id,
            "instance_name": row.instance_name, "environment": row.environment, "config_hash": row.config_hash,
            "first_seen_at": row.first_seen_at.isoformat(), "last_seen_at": row.last_seen_at.isoformat(),
            "current": row.id in current}


def current_configs(session, ids):
    """IDs among `ids` that some currently observed instance runs."""
    if not ids:
        return set()
    return set(session.scalars(select(Instance.config_snapshot_id).where(Instance.config_snapshot_id.in_(ids))))


def config_versions(session, limit, service_id=None, instance=None, before=None):
    """Distinct configurations, newest first by first observation; `next` is an opaque keyset cursor.

    A config that returns after a change is a new version: versions are a timeline, not a set of hashes.
    """
    query = select(ConfigSnapshot)
    if service_id is not None:
        query = query.where(ConfigSnapshot.service_id == service_id)
    if instance is not None:
        query = query.where(ConfigSnapshot.source_id == instance[0], ConfigSnapshot.instance_id == instance[1])
    if before:
        query = query.where(tuple_(ConfigSnapshot.first_seen_at, ConfigSnapshot.id) < parse_cursor(before))
    rows = session.scalars(query.order_by(ConfigSnapshot.first_seen_at.desc(), ConfigSnapshot.id.desc())
                           .limit(limit + 1)).all()
    page = rows[:limit]
    current = current_configs(session, [r.id for r in page])
    return {"versions": [config_version(r, current) for r in page],
            "next": cursor(page[-1], "first_seen_at") if len(rows) > limit else None}


def config_snapshot(session, version_id):
    row = session.get(ConfigSnapshot, version_id)
    return None if row is None else {**config_version(row, current_configs(session, [row.id])), "config": row.config}


def service_known(session, service_id):
    return (session.get(Service, service_id) is not None
            or session.scalar(select(ConfigSnapshot.id).where(ConfigSnapshot.service_id == service_id).limit(1))
            is not None)


def instance_known(session, source_id, instance_id):
    return (session.get(Instance, (source_id, instance_id)) is not None
            or session.scalar(select(ConfigSnapshot.id).where(ConfigSnapshot.source_id == source_id,
                                                              ConfigSnapshot.instance_id == instance_id).limit(1))
            is not None)


class Retention:
    """Deletes history older than GMAN_RETENTION_DAYS, at most once per interval per process."""

    def __init__(self, interval=timedelta(hours=1)):
        self.interval = interval
        self.last = None

    def run(self, engine, now=None):
        now = now or utcnow()
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last = now
        days = int(os.getenv("GMAN_RETENTION_DAYS", "180"))
        if days <= 0:
            return False
        cutoff = now - timedelta(days=days)
        with engine.begin() as connection:
            connection.execute(delete(Event).where(Event.received_at < cutoff))
            # Current configs stay: every accepted snapshot refreshes their last_seen_at.
            connection.execute(delete(ConfigSnapshot).where(ConfigSnapshot.last_seen_at < cutoff))
        return True
