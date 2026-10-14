"""Сущности модуля deployment_report: сведения о деплое из Bamboo и инициатор."""
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]
UNKNOWN = "unknown"


class MissingDeploymentFieldsError(ValueError):
    """Не хватает обязательных сведений о деплое."""


class DeploymentApiError(RuntimeError):
    """GMan API отверг событие деплоя."""


@dataclass(frozen=True)
class DeploymentContext:
    """Сведения о деплое: переменные GMAN_* или переменные, которые Bamboo передает script task."""

    service_id: str
    status: str
    environment: str
    build_id: str
    release: str
    version: str
    results_url: str
    commit: str
    image: str
    image_digest: str
    # Явные переопределения: пусто — значение выводится.
    event_id: str = ""
    deployment_id: str = ""
    occurred_at: str = ""
    actor: str = ""
    actor_kind: str = ""
    trigger_reason: str = ""
    bamboo_user: str = ""


@dataclass(frozen=True)
class Initiator:
    """Кто запустил деплой и какого он вида (human, service-account, schedule, unknown)."""

    actor: str
    kind: str
