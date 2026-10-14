"""Протоколы зависимостей модуля inventory от других модулей (реализует оркестратор releases)."""
from datetime import datetime
from typing import Protocol


class ReleaseTracker(Protocol):
    """Учет релизов по наблюдениям runtime."""

    def track_snapshot(self, event_ids: list[int], scopes: set[tuple[str, str]], now: datetime) -> None:
        """Связывает события снимка с релизами и пересчитывает статусы (в транзакции снимка)."""

    def expire(self, now: datetime) -> None:
        """Завершает по тайм-ауту релизы, раскатка которых не сошлась."""
