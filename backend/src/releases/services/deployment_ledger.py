"""Журнал событий CI: идемпотентный прием по ID события и запись деплоя в историю."""
from datetime import datetime

from database.advisory_lock import AdvisoryLock
from releases.adapters.deployment_repository import DeploymentRepository
from releases.adapters.runtime_event_repository import RuntimeEventRepository
from releases.entity import DeploymentConflictError, DeploymentReport, JsonObject, Release

LOCK_PREFIX = "ci:"
DEPLOYMENT = "deployment"
BAMBOO_ASSERTION = "Bamboo integration assertion"


class DeploymentLedger:
    """Повтор того же шага CI — дубликат; тот же ID с другим содержимым — конфликт."""

    def __init__(self, deployments: DeploymentRepository, events: RuntimeEventRepository,
                 lock: AdvisoryLock) -> None:
        """Запоминает репозитории событий CI и истории и блокировку."""
        self._deployments = deployments
        self._events = events
        self._lock = lock

    def stored_request(self, report: DeploymentReport) -> JsonObject | None:
        """Ранее принятый запрос с тем же ID события (под блокировкой ID) или None."""
        self._lock.acquire(LOCK_PREFIX + report.event_id)
        stored = self._deployments.get(report.event_id)
        return stored

    def ensure_same(self, previous: DeploymentReport, report: DeploymentReport) -> None:
        """Повтор должен совпадать с принятым запросом; иначе DeploymentConflictError."""
        if previous.request != report.request:
            raise DeploymentConflictError("Event ID reused with different payload")

    def record(self, report: DeploymentReport, received_at: datetime) -> int:
        """Сохраняет событие CI и запись истории; без времени события — время приема сервером."""
        self._deployments.add(report.event_id, report.request, received_at)
        received = received_at.isoformat()
        data = {**report.request, "occurred_at": report.request.get("occurred_at") or received, "type": DEPLOYMENT,
                "received_at": received, "actor_source": BAMBOO_ASSERTION}
        event_id = self._events.add(data, received_at, report.occurred_at or received_at)
        return event_id

    def link(self, event_id: int, release: Release) -> None:
        """Связывает запись истории деплоя с его релизом."""
        self._events.link(event_id, release.id)
