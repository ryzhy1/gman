"""Схема БД (ORM-записи). Должна совпадать с миграциями Alembic: это проверяет `alembic check` в тестах.

Снимки, экземпляры и конфигурации хранятся как проверенный JSON, чтобы поля, добавленные в модели API,
проходили без миграции; колонкой становится только то, по чему фильтруют или сортируют.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Identity, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from database.column_types import JSON_DOCUMENT, ROW_ID, UtcDateTime, utc_now

HASH_LENGTH = 64
UNKNOWN = "unknown"
CONFIG_HASH = String(HASH_LENGTH)
# Элементы схемы привязываются к одной колонке, поэтому у каждой колонки свой экземпляр.
CONFIG_SNAPSHOT_ID = Identity()
EVENT_ID = Identity()
RELEASE_ID = Identity()
INSTANCE_SOURCE = ForeignKey("sources.source_id", ondelete="CASCADE")
INSTANCE_CONFIG_SNAPSHOT = ForeignKey("config_snapshots.id", ondelete="SET NULL")


class Base(DeclarativeBase):
    """Базовый класс ORM-записей GMan."""


class ServiceRecord(Base):
    """Сервис каталога."""

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
    # Поля модели API, у которых еще нет своей колонки (например, logs).
    extra: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class SourceRecord(Base):
    """Последний принятый снимок Docker-хоста или кластера Kubernetes, без экземпляров."""

    __tablename__ = "sources"
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_type: Mapped[str] = mapped_column(String)
    hostname: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)
    snapshot: Mapped[dict] = mapped_column(JSON_DOCUMENT)


class ConfigSnapshotRecord(Base):
    """Одна запись на каждую отличающуюся конфигурацию экземпляра — для сравнения версий."""

    __tablename__ = "config_snapshots"
    id: Mapped[int] = mapped_column(ROW_ID, CONFIG_SNAPSHOT_ID, primary_key=True)
    source_id: Mapped[str] = mapped_column(String)
    instance_id: Mapped[str] = mapped_column(String)
    service_id: Mapped[str] = mapped_column(String, default="")
    # Подписи на момент первого наблюдения: история читаема и после исчезновения экземпляра.
    instance_name: Mapped[str] = mapped_column(String, default="", server_default="")
    environment: Mapped[str] = mapped_column(String, default="", server_default="")
    config_hash: Mapped[str] = mapped_column(CONFIG_HASH)
    config: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)

    __table_args__ = (Index("ix_config_snapshots_instance", "source_id", "instance_id", "first_seen_at"),
                      Index("ix_config_snapshots_service", "service_id", "first_seen_at"),
                      Index("ix_config_snapshots_last_seen", "last_seen_at"))


class InstanceRecord(Base):
    """Наблюдаемые сейчас экземпляры; заменяются каждым принятым снимком."""

    __tablename__ = "instances"
    source_id: Mapped[str] = mapped_column(String, INSTANCE_SOURCE, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    # Порядок в снимке, как его прислал агент.
    position: Mapped[int] = mapped_column(Integer)
    service_id: Mapped[str] = mapped_column(String, default="")
    environment: Mapped[str] = mapped_column(String, default="")
    runtime: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    config_hash: Mapped[str] = mapped_column(CONFIG_HASH)
    config_snapshot_id: Mapped[int | None] = mapped_column(ROW_ID, INSTANCE_CONFIG_SNAPSHOT, nullable=True)
    data: Mapped[dict] = mapped_column(JSON_DOCUMENT)

    config_snapshot: Mapped[ConfigSnapshotRecord | None] = relationship()

    __table_args__ = (Index("ix_instances_service_id", "service_id"),)


class EventRecord(Base):
    """Запись аудита (истории изменений)."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(ROW_ID, EVENT_ID, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)
    occurred_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    type: Mapped[str] = mapped_column(String)
    service_id: Mapped[str] = mapped_column(String, default="")
    source_id: Mapped[str] = mapped_column(String, default="")
    instance_id: Mapped[str] = mapped_column(String, default="")
    # Релиз, с которым событие связано; дублируется в data["release_id"].
    release_id: Mapped[int | None] = mapped_column(ROW_ID, nullable=True)
    data: Mapped[dict] = mapped_column(JSON_DOCUMENT)

    __table_args__ = (Index("ix_events_received", received_at.desc(), id.desc()),
                      Index("ix_events_service", "service_id", received_at.desc(), id.desc()),
                      Index("ix_events_release", "release_id"))


class RuntimeEventMarkRecord(Base):
    """Последние учтенные счетчик и время каждого Event Kubernetes, чтобы повторы записывались один раз.

    Event узла хранит также узел и причину: последнее время по узлу и причине экспортируется на /metrics
    и показывается на карточке узла. Пустой узел — Event pod-а.
    """

    __tablename__ = "runtime_event_marks"
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    uid: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    last_at: Mapped[datetime] = mapped_column(UtcDateTime)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)
    node: Mapped[str] = mapped_column(String, default="", server_default="")
    reason: Mapped[str] = mapped_column(String, default="", server_default="")

    __table_args__ = (Index("ix_runtime_event_marks_received", "received_at"),
                      Index("ix_runtime_event_marks_node", "source_id", "node", "reason"))


class DeploymentRecord(Base):
    """События CI по ID — для идемпотентных повторов."""

    __tablename__ = "deployments"
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    request: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)


class ReleaseRecord(Base):
    """Деплой Bamboo одного сервиса в окружение; статус выводится из наблюдений runtime."""

    __tablename__ = "releases"
    id: Mapped[int] = mapped_column(ROW_ID, RELEASE_ID, primary_key=True)
    service_id: Mapped[str] = mapped_column(String)
    environment: Mapped[str] = mapped_column(String)
    # ID результата деплоя Bamboo, если известен, иначе "<build_id>|<release>".
    key: Mapped[str] = mapped_column(String)
    release: Mapped[str] = mapped_column(String, default="")
    version: Mapped[str] = mapped_column(String, default="")
    build_id: Mapped[str] = mapped_column(String, default="")
    build_url: Mapped[str] = mapped_column(String, default="")
    commit: Mapped[str] = mapped_column(String, default="")
    image: Mapped[str] = mapped_column(String, default="")
    image_digest: Mapped[str] = mapped_column(String, default="")
    initiator: Mapped[str] = mapped_column(String, default=UNKNOWN)
    initiator_kind: Mapped[str] = mapped_column(String, default=UNKNOWN)
    status: Mapped[str] = mapped_column(String)
    status_reason: Mapped[str] = mapped_column(String, default="")
    bamboo_status: Mapped[str] = mapped_column(String, default="")
    correlation: Mapped[str] = mapped_column(String, default="")
    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # Изменения runtime с этого момента могут относиться к релизу (расхождение часов, runtime раньше Bamboo).
    window_start: Mapped[datetime] = mapped_column(UtcDateTime)
    rollout_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    bamboo_finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(ROW_ID, nullable=True)
    progress: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)

    __table_args__ = (Index("ix_releases_service", "service_id", "environment", started_at.desc(), id.desc()),
                      Index("ix_releases_key", "service_id", "environment", "key"),
                      Index("ix_releases_status", "status", "started_at"))
