"""Оркестратор модуля inventory: прием снимков, каталог сервисов, обзор, аудит и ссылки на логи."""
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from inventory.entity import JsonObject, ObservationContext, SnapshotIntake, SnapshotReport, UnknownServiceError
from inventory.mappers import (catalog_service_from_payload, catalog_service_to_response, history_page_to_response,
                               ingest_response, log_targets_from_sources, observation_context,
                               overview_to_response, snapshot_from_payload, splunk_links_to_response,
                               splunk_override_from_service)
from inventory.ports import ReleaseTracker
from inventory.services.history_builder import SnapshotHistoryBuilder
from inventory.services.history_gap_detector import HistoryGapDetector
from inventory.services.history_journal import HistoryJournal
from inventory.services.housekeeping import Housekeeping
from inventory.services.rollout_scope import RolloutScope
from inventory.services.runtime_event_recorder import RuntimeEventRecorder
from inventory.services.service_catalog import ServiceCatalog
from inventory.services.snapshot_intake import SnapshotIntakeService
from inventory.services.source_overview import SourceOverview
from inventory.services.splunk_links import SplunkLinkBuilder

Transaction = Callable[[], AbstractContextManager[None]]
OVERVIEW_EVENTS = 200


@dataclass(frozen=True)
class InventoryDependencies:
    """Готовые зависимости оркестратора inventory."""

    transaction: Transaction
    intake: SnapshotIntakeService
    history_builder: SnapshotHistoryBuilder
    gap_detector: HistoryGapDetector
    runtime_events: RuntimeEventRecorder
    journal: HistoryJournal
    rollout_scope: RolloutScope
    releases: ReleaseTracker
    housekeeping: Housekeeping
    catalog: ServiceCatalog
    source_overview: SourceOverview
    splunk_links: SplunkLinkBuilder
    # Показываемые порталу настройки: адрес Grafana и темп обновления.
    portal_settings: dict[str, Any]


class InventoryOrchestrator:
    """Единственная точка входа модуля inventory."""

    def __init__(self, dependencies: InventoryDependencies) -> None:
        """Запоминает зависимости модуля."""
        self._deps = dependencies

    def ingest(self, payload: JsonObject) -> JsonObject:
        """Принимает снимок источника и записывает изменения в историю."""
        # Пейлоад -> снимок (повтор ID экземпляров — ошибка).
        report = snapshot_from_payload(payload)
        received_at = datetime.now(timezone.utc)
        with self._deps.transaction():
            # Сохраняем снимок, если он новее последнего принятого (повтор агента — не ошибка).
            intake = self._deps.intake.accept(report, received_at)
            if intake is None:
                response = ingest_response(accepted=False)
                return response
            # Записываем историю снимка.
            context = observation_context(report, received_at)
            change_ids = self._record_history(context, report, intake)
            # Пересчитываем релизы сервисов, чьи экземпляры изменились.
            scopes = self._deps.rollout_scope.touched(intake.previous_instances, report.instances)
            self._deps.releases.track_snapshot(change_ids, scopes, received_at)
        # После фиксации снимка — обслуживание: тайм-ауты релизов и очистка старой истории.
        self.run_housekeeping(received_at)
        response = ingest_response(accepted=True)
        return response

    def run_housekeeping(self, now: datetime) -> bool:
        """Обслуживание, если пришло время; True — история очищалась."""
        purged = self._deps.housekeeping.run(now)
        return purged

    def register_service(self, service_id: str, payload: JsonObject) -> JsonObject:
        """Создает или обновляет сервис каталога."""
        # Пейлоад -> сервис (ID в пути и теле совпадают).
        service = catalog_service_from_payload(service_id, payload)
        # Проверяем шаблон поиска Splunk из карточки.
        override = splunk_override_from_service(service)
        self._deps.splunk_links.check_override(override)
        received_at = datetime.now(timezone.utc)
        with self._deps.transaction():
            # Сохраняем сервис и записываем изменение в историю.
            self._deps.catalog.register(service, received_at)
        response = catalog_service_to_response(service)
        return response

    def overview(self) -> JsonObject:
        """Обзор портала: каталог, источники с экземплярами и последние события."""
        now = datetime.now(timezone.utc)
        with self._deps.transaction():
            # Читаем каталог, источники (со сводкой Events узлов) и последние события.
            services = self._deps.catalog.services()
            sources = self._deps.source_overview.sources(now)
            events = self._deps.journal.page(OVERVIEW_EVENTS)
        # Собираем ответ.
        response = overview_to_response(services, sources, events, self._deps.portal_settings, now)
        return response

    def history(self, limit: int, service_id: str | None, before: str) -> JsonObject:
        """Страница аудита, новые первыми."""
        with self._deps.transaction():
            # Читаем страницу (неразборчивый курсор — ошибка).
            page = self._deps.journal.page(limit, service_id, before)
        response = history_page_to_response(page)
        return response

    def splunk_links(self, service_id: str, environment: str, range_name: str) -> JsonObject:
        """Ссылки на поиск Splunk для сервиса и его экземпляров; значения экранируются в SPL здесь."""
        with self._deps.transaction():
            # Находим сервис каталога (неизвестный — ошибка) и все наблюдаемые экземпляры.
            service = self._deps.catalog.get(service_id)
            if service is None:
                raise UnknownServiceError(service_id)
            documents = self._deps.source_overview.documents()
        # Источники -> цели ссылок; карточка -> переопределение индекса и шаблона.
        targets = log_targets_from_sources(documents)
        override = splunk_override_from_service(service)
        # Строим ссылки.
        links = self._deps.splunk_links.links(service.id, override, targets, environment, range_name)
        response = splunk_links_to_response(links)
        return response

    def _record_history(self, context: ObservationContext, report: SnapshotReport,
                        intake: SnapshotIntake) -> list[int]:
        """Пишет события снимка; возвращает ID изменений экземпляров (их сопоставляют с релизами)."""
        # Первый снимок: одно событие подключения источника вместо N изменений.
        if intake.first:
            connected = self._deps.history_builder.connected(context, len(report.instances))
            self._deps.journal.record([connected])
            change_ids = []
        else:
            # Изменения экземпляров, затем разрыв истории (молчание источника, переполнение spool агента).
            changes = self._deps.history_builder.instance_changes(context, intake.previous_instances,
                                                                  report.instances)
            change_ids = self._deps.journal.record(changes)
            gaps = self._deps.gap_detector.detect(context, intake.previous_observed_at, report,
                                                  intake.previous_instances)
            self._deps.journal.record(gaps)
        # Events Kubernetes: каждое новое повторение один раз.
        runtime_events = self._deps.runtime_events.record(context, report.events)
        self._deps.journal.record(runtime_events)
        return change_ids
