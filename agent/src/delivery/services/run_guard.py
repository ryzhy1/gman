"""Запуски агента для одного источника не перекрываются."""
from pathlib import Path

from delivery.adapters.run_lock import RunLock


class RunGuard:
    """Держит блокировку источника до конца процесса."""

    def __init__(self, lock: RunLock, path: Path) -> None:
        """Запоминает адаптер блокировки и файл блокировки источника."""
        self._lock = lock
        self._path = path
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        """Берет блокировку; False — другой запуск еще идет."""
        self._descriptor = self._lock.acquire(self._path)
        acquired = self._descriptor is not None
        return acquired
