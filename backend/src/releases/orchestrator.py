"""Оркестратор модуля releases: события деплоя Bamboo, учет релизов по наблюдениям runtime и их чтение.

Оценка идет при приеме (снимков и событий Bamboo, под блокировкой пары сервис/окружение) и при периодическом
обслуживании после снимков (тайм-ауты). Она идемпотентна: повторы снимков и событий отсекаются раньше,
а повторная оценка тех же данных ничего не меняет.
"""
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from releases.entity import JsonObject
from releases.mappers import (deployment_from_payload, deployment_response, detail_to_response, page_to_response,
                              release_filter_from_params)
from releases.services.deployment_ledger import DeploymentLedger
from releases.services.release_reader import ReleaseReader
from releases.services.release_registry import ReleaseRegistry
from releases.services.release_supersession import ReleaseSupersession
from releases.services.release_tracking import ReleaseTracking

Transaction = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True)
class ReleasesDependencies:
    """Готовые зависимости оркестратора releases."""

    transaction: Transaction
    ledger: DeploymentLedger
    registry: ReleaseRegistry
    supersession: ReleaseSupersession
    tracking: ReleaseTracking
    reader: ReleaseReader


class ReleasesOrchestrator:
    """Единственная точка входа модуля releases (для API и для модуля inventory)."""

    def __init__(self, dependencies: ReleasesDependencies) -> None:
        """Запоминает зависимости модуля."""
        self._deps = dependencies

    def record_deployment(self, payload: JsonObject) -> JsonObject:
        """Принимает событие деплоя Bamboo и создает или обновляет релиз (в одной транзакции с событием)."""
        # Пейлоад -> событие CI.
        report = deployment_from_payload(payload)
        received_at = datetime.now(timezone.utc)
        with self._deps.transaction():
            # Повтор того же шага CI: дубликат, если содержимое то же, иначе конфликт.
            stored = self._deps.ledger.stored_request(report)
            if stored is not None:
                previous = deployment_from_payload(stored)
                self._deps.ledger.ensure_same(previous, report)
                response = deployment_response(None)
                return response
            # Записываем событие CI и запись истории.
            event_id = self._deps.ledger.record(report, received_at)
            # Создаем или обновляем релиз по отчету Bamboo и связываем с ним запись истории.
            self._deps.tracking.lock_scope(report.service_id, report.environment)
            release, created = self._deps.registry.apply(report, received_at)
            self._deps.ledger.link(event_id, release)
            # Новый релиз вытесняет незавершенные предыдущие.
            if created:
                self._deps.supersession.supersede(release, received_at)
            # Открытый релиз забирает уже записанные изменения runtime своего окна, затем пересчитывается.
            self._deps.tracking.correlate_window(release)
            self._deps.tracking.evaluate(release, received_at)
        response = deployment_response(release.id)
        return response

    def track_snapshot(self, event_ids: list[int], scopes: set[tuple[str, str]], now: datetime) -> None:
        """После снимка (в его транзакции): связывает события с релизами и пересчитывает затронутые пары."""
        with self._deps.transaction():
            for service_id, environment in sorted(scopes):
                # Пересчитываем открытые релизы пары под ее блокировкой.
                self._deps.tracking.track_scope(service_id, environment, event_ids, now)

    def expire(self, now: datetime) -> None:
        """Периодически: завершает по тайм-ауту релизы, раскатка которых не сошлась."""
        with self._deps.transaction():
            # Пересчитываем открытые релизы старше тайм-аута.
            self._deps.tracking.expire(now)

    def releases(self, params: dict[str, Any]) -> JsonObject:
        """Страница релизов, новые первыми по началу."""
        # Параметры -> отбор.
        release_filter = release_filter_from_params(params)
        now = datetime.now(timezone.utc)
        with self._deps.transaction():
            # Читаем страницу (неразборчивый курсор — ошибка).
            page = self._deps.reader.page(release_filter)
        response = page_to_response(page, now)
        return response

    def release(self, release_id: int) -> JsonObject:
        """Релиз со связанными событиями и текущими экземплярами раскатки."""
        now = datetime.now(timezone.utc)
        with self._deps.transaction():
            # Читаем релиз (отсутствующий — ошибка), события и экземпляры с оценкой.
            detail = self._deps.reader.detail(release_id)
        response = detail_to_response(detail, now)
        return response
