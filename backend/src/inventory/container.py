"""Сборка модуля inventory: репозитории, сервисы, настройки ссылок Splunk и оркестратор."""
import logging
from dataclasses import dataclass
from datetime import timedelta

from config.settings import Settings
from database.advisory_lock import AdvisoryLock
from database.unit_of_work import UnitOfWork
from inventory.adapters.catalog_repository import CatalogRepository
from inventory.adapters.event_mark_repository import EventMarkRepository
from inventory.adapters.event_repository import EventRepository
from inventory.adapters.instance_repository import InstanceRepository
from inventory.adapters.retention_purger import RetentionPurger
from inventory.adapters.source_repository import SourceRepository
from inventory.entity import SplunkSettings, SplunkTemplateError
from inventory.orchestrator import InventoryDependencies, InventoryOrchestrator
from inventory.ports import ReleaseTracker
from inventory.services.field_changes import FieldChangeDetector
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
from inventory.services.splunk_query import SplunkQuery
from inventory.services.splunk_settings import SplunkSettingsParser

logger = logging.getLogger(__name__)

# Events узлов, сводка которых показывается на карточке узла; полный список — в аудите.
NODE_EVENTS_WINDOW = timedelta(hours=24)
PURGE_INTERVAL = timedelta(hours=1)
RELEASE_EXPIRY_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class InventoryConfig:
    """Узкая конфигурация модуля inventory."""

    history_gap_seconds: float
    retention_days: int
    grafana_url: str
    refresh_seconds: int
    splunk_url: str
    splunk_search: str
    splunk_indexes: str


def inventory_config(settings: Settings) -> InventoryConfig:
    """Выделяет из общих настроек то, что нужно модулю inventory."""
    config = InventoryConfig(history_gap_seconds=settings.history_gap_seconds, retention_days=settings.retention_days,
                             grafana_url=settings.grafana_url, refresh_seconds=settings.metrics_interval_seconds,
                             splunk_url=settings.splunk_url, splunk_search=settings.splunk_search,
                             splunk_indexes=settings.splunk_indexes)
    return config


@dataclass(frozen=True)
class _Repositories:
    """Адаптеры хранилища модуля inventory."""

    lock: AdvisoryLock
    sources: SourceRepository
    instances: InstanceRepository
    marks: EventMarkRepository
    events: EventRepository
    catalog: CatalogRepository
    purger: RetentionPurger


def build_inventory_orchestrator(settings: Settings, unit_of_work: UnitOfWork,
                                 releases: ReleaseTracker) -> InventoryOrchestrator:
    """Создает адаптеры, сервисы и оркестратор; учет релизов приходит из модуля releases."""
    config = inventory_config(settings)
    repositories = _repositories(unit_of_work)
    dependencies = _dependencies(config, unit_of_work, repositories, releases)
    orchestrator = InventoryOrchestrator(dependencies)
    return orchestrator


def _repositories(unit_of_work: UnitOfWork) -> _Repositories:
    """Адаптеры хранилища поверх общей границы транзакций."""
    lock = AdvisoryLock(unit_of_work)
    sources = SourceRepository(unit_of_work)
    instances = InstanceRepository(unit_of_work)
    marks = EventMarkRepository(unit_of_work)
    events = EventRepository(unit_of_work)
    catalog = CatalogRepository(unit_of_work)
    purger = RetentionPurger(unit_of_work)
    repositories = _Repositories(lock=lock, sources=sources, instances=instances, marks=marks, events=events,
                                 catalog=catalog, purger=purger)
    return repositories


def _dependencies(config: InventoryConfig, unit_of_work: UnitOfWork, repositories: _Repositories,
                  releases: ReleaseTracker) -> InventoryDependencies:
    """Сервисы модуля и пакет зависимостей оркестратора."""
    change_detector = FieldChangeDetector()
    journal = HistoryJournal(repositories.events)
    intake = SnapshotIntakeService(repositories.sources, repositories.instances, repositories.lock)
    history_builder = SnapshotHistoryBuilder(change_detector)
    gap_detector = HistoryGapDetector(config.history_gap_seconds)
    runtime_events = RuntimeEventRecorder(repositories.marks)
    housekeeping = Housekeeping(repositories.purger, releases, config.retention_days, PURGE_INTERVAL,
                                RELEASE_EXPIRY_INTERVAL)
    catalog = ServiceCatalog(repositories.catalog, repositories.lock, change_detector, journal)
    source_overview = SourceOverview(repositories.sources, repositories.marks, NODE_EVENTS_WINDOW)
    splunk_links = _splunk_links(config)
    rollout_scope = RolloutScope()
    portal_settings = {"grafana_url": config.grafana_url, "refresh_seconds": config.refresh_seconds}
    dependencies = InventoryDependencies(
        transaction=unit_of_work.transaction, intake=intake, history_builder=history_builder,
        gap_detector=gap_detector, runtime_events=runtime_events, journal=journal, rollout_scope=rollout_scope,
        releases=releases, housekeeping=housekeeping, catalog=catalog, source_overview=source_overview,
        splunk_links=splunk_links, portal_settings=portal_settings)
    return dependencies


def _splunk_links(config: InventoryConfig) -> SplunkLinkBuilder:
    """Ссылки Splunk; некорректная конфигурация не роняет API, а выключает ссылки с сообщением в ответе."""
    query = SplunkQuery()
    parser = SplunkSettingsParser(query)
    settings: SplunkSettings | None = None
    error: str | None = None
    try:
        settings = parser.parse(config.splunk_url, config.splunk_search, config.splunk_indexes)
    except SplunkTemplateError as problem:
        logger.exception("Splunk links disabled: invalid GMAN_SPLUNK_* configuration")
        error = str(problem)
    builder = SplunkLinkBuilder(query, settings, error)
    return builder
