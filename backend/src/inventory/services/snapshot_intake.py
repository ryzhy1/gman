"""Прием снимка источника: сериализация по источнику, отбрасывание повторов и сохранение состояния."""
import hashlib
import json
from datetime import datetime

from database.advisory_lock import AdvisoryLock
from inventory.adapters.instance_repository import InstanceRepository
from inventory.adapters.source_repository import SourceRepository
from inventory.entity import JsonObject, ObservedInstance, SnapshotIntake, SnapshotReport

LAST_OOM_AT = "last_oom_at"
COMPACT_SEPARATORS = (",", ":")


class SnapshotIntakeService:
    """Принимает снимок, если он новее последнего принятого, и заменяет им состояние источника."""

    def __init__(self, sources: SourceRepository, instances: InstanceRepository, lock: AdvisoryLock) -> None:
        """Запоминает репозитории источников и экземпляров и блокировку."""
        self._sources = sources
        self._instances = instances
        self._lock = lock

    def accept(self, report: SnapshotReport, received_at: datetime) -> SnapshotIntake | None:
        """Сохраняет снимок; повтор или более старый снимок — None (идемпотентный повтор агента)."""
        self._lock.acquire(report.source_id)
        previous_observed_at = self._sources.observed_at(report.source_id)
        if previous_observed_at is not None and previous_observed_at >= report.observed_at:
            return None
        previous = self._instances.current(report.source_id)
        for instance in report.instances:
            if instance.id in previous:
                _keep_latest_oom(instance, previous[instance.id])
        hashes = [config_hash(instance.config) for instance in report.instances]
        self._sources.save(report, received_at)
        self._instances.replace(report.source_id, report.instances, hashes, set(previous), received_at)
        intake = SnapshotIntake(previous_observed_at=previous_observed_at, previous_instances=previous)
        return intake


def config_hash(config: JsonObject) -> str:
    """Отпечаток конфигурации: одинаковые конфигурации — одна версия."""
    canonical = json.dumps(config, sort_keys=True, separators=COMPACT_SEPARATORS)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest


def _keep_latest_oom(instance: ObservedInstance, before: ObservedInstance) -> None:
    """Прошлый факт, который runtime забывает, остается, пока не придет более новый.

    Docker сбрасывает OOMKilled при перезапуске, а перезапуск агента теряет время события `oom`.
    """
    old, new = before.last_oom_at, instance.last_oom_at
    if old and (not new or datetime.fromisoformat(new) < datetime.fromisoformat(old)):
        instance.document[LAST_OOM_AT] = old
