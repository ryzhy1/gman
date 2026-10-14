"""Каталог сервисов: регистрация с записью изменений в историю и чтение."""
from datetime import datetime

from database.advisory_lock import AdvisoryLock
from inventory.adapters.catalog_repository import CatalogRepository
from inventory.entity import UNKNOWN, CatalogService, HistoryEvent, JsonObject
from inventory.services.field_changes import FieldChangeDetector
from inventory.services.history_journal import HistoryJournal

SERVICE_CREATED = "service-created"
SERVICE_UPDATED = "service-updated"
PORTAL = "portal"
LOCK_PREFIX = "service:"
CORE_FIELDS = ("id", "name", "description", "owner", "team", "repository", "grafana", "splunk", "bamboo")


class ServiceCatalog:
    """Сервисы каталога; без входа автор изменения каталога не известен."""

    def __init__(self, repository: CatalogRepository, lock: AdvisoryLock, change_detector: FieldChangeDetector,
                 journal: HistoryJournal) -> None:
        """Запоминает репозиторий каталога, блокировку, сравнение документов и журнал истории."""
        self._repository = repository
        self._lock = lock
        self._changes = change_detector
        self._journal = journal

    def register(self, service: CatalogService, received_at: datetime) -> None:
        """Создает или обновляет сервис; создание и непустое изменение пишутся в историю."""
        self._lock.acquire(LOCK_PREFIX + service.id)
        before = self._repository.get(service.id)
        self._repository.put(service)
        before_document = service_document(before) if before is not None else {}
        changes = self._changes.changes(before_document, service_document(service))
        if before is not None and not changes:
            return
        data = {"type": SERVICE_CREATED if before is None else SERVICE_UPDATED, "service_id": service.id,
                "actor": UNKNOWN, "actor_kind": UNKNOWN, "actor_source": PORTAL,
                "occurred_at": received_at.isoformat(), "received_at": received_at.isoformat(), "changes": changes}
        event = HistoryEvent(data=data, received_at=received_at, occurred_at=received_at)
        self._journal.record([event])

    def get(self, service_id: str) -> CatalogService | None:
        """Сервис по ID или None."""
        service = self._repository.get(service_id)
        return service

    def services(self) -> list[CatalogService]:
        """Все сервисы каталога по ID."""
        services = self._repository.list_all()
        return services


def service_document(service: CatalogService) -> JsonObject:
    """Сервис как документ API: основные поля и необязательные разделы."""
    document = {name: getattr(service, name) for name in CORE_FIELDS}
    document.update(service.extra)
    return document
