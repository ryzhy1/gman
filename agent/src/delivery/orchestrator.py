"""Оркестратор модуля delivery: блокировка запуска, spool и доставка снимков в GMan API."""
from collections.abc import Callable
from dataclasses import dataclass

from delivery.entity import JsonObject, SubmissionPendingError
from delivery.services.delivery_retry import DeliveryRetry
from delivery.services.run_guard import RunGuard
from delivery.services.snapshot_spool import SnapshotSpool


@dataclass(frozen=True)
class DeliveryDependencies:
    """Готовые зависимости оркестратора delivery."""

    run_guard: RunGuard
    spool: SnapshotSpool
    retry: DeliveryRetry


class DeliveryOrchestrator:
    """Единственная точка входа модуля delivery; неудачный сбор никогда не отправляется пустым снимком."""

    def __init__(self, dependencies: DeliveryDependencies) -> None:
        """Запоминает зависимости модуля."""
        self._deps = dependencies

    def acquire_lock(self) -> bool:
        """Берет блокировку источника на время процесса; занята — другой запуск еще идет."""
        acquired = self._deps.run_guard.acquire()
        return acquired

    def run_once(self, collect: Callable[[], JsonObject]) -> None:
        """Один запуск по таймеру: собрать, положить в spool и отправить все накопленное по порядку."""
        # Собираем снимок. Любой сбой сбора — не пустой «авторитетный» снимок: накопленные ранее снимки
        # все равно отправляются, а ошибка пробрасывается дальше.
        try:
            snapshot = collect()
        except Exception:
            self._deps.spool.flush(self._deps.retry.deliver)
            raise
        # Кладем снимок в spool и отправляем все по порядку; недоставленное остается до следующего запуска.
        remaining = self.submit([snapshot])
        if remaining:
            raise SubmissionPendingError(remaining)

    def submit(self, snapshots: list[JsonObject]) -> int:
        """Кладет снимки в spool и отправляет все накопленные; возвращает число оставшихся."""
        for snapshot in snapshots:
            self._deps.spool.put(snapshot)
        remaining = self._deps.spool.flush(self._deps.retry.deliver)
        return remaining
