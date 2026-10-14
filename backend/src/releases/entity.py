"""Сущности модуля releases: релиз, отчет Bamboo, экземпляр раскатки и событие runtime.

Релиз — один деплой Bamboo сервиса в окружение. Его статус выводится, а не утверждается: Bamboo сообщает, что
что-то задеплоил, а inventory показывает, работает ли каждый экземпляр сервиса в окружении на целевом образе
и здоров ли он.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

JsonObject = dict[str, Any]
UNKNOWN = "unknown"

# Bamboo сообщил о деплое; ни один экземпляр еще не работает на целевом образе.
STARTED = "started"
# Хотя бы один экземпляр работает на целевом образе (по digest, иначе по версии/тегу).
ROLLING_OUT = "rolling-out"
# Все экземпляры образа в сервисе/окружении на целевой версии и здоровы, хотя бы один создан после начала
# релиза (или Bamboo сообщил об успехе).
SUCCEEDED = "succeeded"
# Bamboo сообщил об ошибке, новый экземпляр падает или убит OOM, либо раскатка не сошлась за тайм-аут.
FAILED = "failed"
# Более новый релиз того же сервиса/окружения начался раньше, чем этот завершился.
SUPERSEDED = "superseded"
OPEN_STATUSES = (STARTED, ROLLING_OUT)
# Изменения runtime, которые могут относиться к релизу.
RUNTIME_TYPES = ("instance-appeared", "instance-removed", "config-changed", "instance-restarted", "state-changed")
STATUSES = (STARTED, ROLLING_OUT, SUCCEEDED, FAILED, SUPERSEDED)


class ReleaseError(Exception):
    """Базовая ошибка модуля releases."""


class DeploymentConflictError(ReleaseError):
    """ID события CI повторно использован с другим содержимым."""


class ReleaseNotFoundError(ReleaseError):
    """Релиза с таким ID нет."""


class InvalidCursorError(ReleaseError):
    """Курсор страницы релизов не удалось разобрать."""


@dataclass(frozen=True)
class DeploymentReport:
    """Утверждение Bamboo о деплое (запрос как сохранен, с полями по умолчанию)."""

    event_id: str
    service_id: str
    environment: str
    status: str
    build_id: str
    occurred_at: datetime | None
    request: JsonObject

    def field(self, name: str) -> str:
        """Строковое поле запроса (пусто, если не задано)."""
        value = self.request.get(name) or ""
        return value


@dataclass
class Release:
    """Релиз сервиса в окружении."""

    service_id: str
    environment: str
    key: str
    started_at: datetime
    window_start: datetime
    id: int | None = None
    release: str = ""
    version: str = ""
    build_id: str = ""
    build_url: str = ""
    commit: str = ""
    image: str = ""
    image_digest: str = ""
    initiator: str = UNKNOWN
    initiator_kind: str = UNKNOWN
    status: str = STARTED
    status_reason: str = ""
    bamboo_status: str = ""
    correlation: str = ""
    rollout_started_at: datetime | None = None
    bamboo_finished_at: datetime | None = None
    finished_at: datetime | None = None
    superseded_by: int | None = None
    progress: dict[str, int] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        """Релиз еще не завершен."""
        is_open = self.status in OPEN_STATUSES
        return is_open

    def finish(self, status: str, reason: str, at: datetime) -> None:
        """Завершает релиз с итоговым статусом и причиной."""
        self.status = status
        self.status_reason = reason
        self.finished_at = at


@dataclass(frozen=True)
class RolloutInstance:
    """Наблюдаемый экземпляр сервиса в окружении — с полями, по которым судят о раскатке."""

    source_id: str
    id: str
    name: str
    environment: str
    node: str
    status: str
    health: str
    ready: bool | None
    reason: str
    exit_code: int | None
    last_termination_reason: str
    restarts: int
    image: str
    image_id: str
    image_version: str
    app_version: str
    created_at: str | None
    started_at: str | None


@dataclass
class RuntimeEvent:
    """Событие истории об изменении runtime, которое можно связать с релизом."""

    id: int
    type: str
    service_id: str
    occurred_at: datetime | None
    release_id: int | None
    data: JsonObject
    before: RolloutInstance | None
    after: RolloutInstance | None


@dataclass(frozen=True)
class ReleaseFilter:
    """Отбор страницы релизов."""

    limit: int
    service_id: str | None = None
    environment: str | None = None
    status: str | None = None
    before: str = ""


@dataclass(frozen=True)
class ReleasePage:
    """Страница релизов (новые первыми по началу) и курсор следующей."""

    releases: list[Release]
    next_cursor: str | None


@dataclass(frozen=True)
class InstanceVerdict:
    """Экземпляр раскатки с оценкой: на целевой версии ли он, здоров ли, новый ли, есть ли сбой."""

    instance: RolloutInstance
    on_target: str | None
    healthy: bool
    new: bool
    crash: str | None


@dataclass(frozen=True)
class ReleaseDetail:
    """Релиз со связанными событиями и текущими экземплярами раскатки."""

    release: Release
    events: list[JsonObject]
    instances: list[InstanceVerdict]
