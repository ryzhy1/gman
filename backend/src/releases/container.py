"""Сборка модуля releases: репозитории, сервисы учета релизов и оркестратор."""
from dataclasses import dataclass
from datetime import timedelta

from config.settings import Settings
from database.advisory_lock import AdvisoryLock
from database.unit_of_work import UnitOfWork
from releases.adapters.deployment_repository import DeploymentRepository
from releases.adapters.release_repository import ReleaseRepository
from releases.adapters.rollout_instance_repository import RolloutInstanceRepository
from releases.adapters.runtime_event_repository import RuntimeEventRepository
from releases.orchestrator import ReleasesDependencies, ReleasesOrchestrator
from releases.services.deployment_ledger import DeploymentLedger
from releases.services.event_correlator import EventCorrelator
from releases.services.image_matcher import ImageMatcher
from releases.services.instance_health import InstanceHealth
from releases.services.release_evaluator import ReleaseEvaluator
from releases.services.release_reader import ReleaseReader
from releases.services.release_registry import ReleaseRegistry
from releases.services.release_supersession import ReleaseSupersession
from releases.services.release_tracking import ReleaseTracking


@dataclass(frozen=True)
class ReleasesConfig:
    """Узкая конфигурация модуля: тайм-аут раскатки, запас окна до начала и порог перезапусков."""

    timeout: timedelta
    lead: timedelta
    crash_restarts: int


def releases_config(settings: Settings) -> ReleasesConfig:
    """Выделяет из общих настроек параметры оценки релизов."""
    config = ReleasesConfig(timeout=timedelta(minutes=settings.release_timeout_minutes),
                            lead=timedelta(minutes=settings.release_lead_minutes),
                            crash_restarts=settings.release_crash_restarts)
    return config


def build_releases_orchestrator(settings: Settings, unit_of_work: UnitOfWork) -> ReleasesOrchestrator:
    """Создает адаптеры, сервисы и оркестратор поверх общей границы транзакций."""
    config = releases_config(settings)
    lock = AdvisoryLock(unit_of_work)
    releases = ReleaseRepository(unit_of_work)
    instances = RolloutInstanceRepository(unit_of_work)
    events = RuntimeEventRepository(unit_of_work)
    deployments = DeploymentRepository(unit_of_work)
    matcher = ImageMatcher()
    health = InstanceHealth(config.crash_restarts)
    correlator = EventCorrelator(matcher, config.timeout)
    evaluator = ReleaseEvaluator(matcher, health, config.timeout)
    ledger = DeploymentLedger(deployments, events, lock)
    registry = ReleaseRegistry(releases, config.lead, config.timeout)
    supersession = ReleaseSupersession(releases)
    tracking = ReleaseTracking(releases, instances, events, lock, correlator, evaluator, config.timeout)
    reader = ReleaseReader(releases, instances, events, matcher, health)
    dependencies = ReleasesDependencies(transaction=unit_of_work.transaction, ledger=ledger, registry=registry,
                                        supersession=supersession, tracking=tracking, reader=reader)
    orchestrator = ReleasesOrchestrator(dependencies)
    return orchestrator
