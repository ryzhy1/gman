"""Оркестратор модуля config_history: версии конфигураций и их сравнение."""
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from config_history.mappers import comparison_to_response, page_to_response, version_to_response
from config_history.services.version_catalog import VersionCatalog

Transaction = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True)
class ConfigHistoryDependencies:
    """Готовые зависимости оркестратора config_history."""

    transaction: Transaction
    version_catalog: VersionCatalog


class ConfigHistoryOrchestrator:
    """Единственная точка входа модуля config_history; каждый сценарий — одна транзакция чтения."""

    def __init__(self, dependencies: ConfigHistoryDependencies) -> None:
        """Запоминает границу транзакции и сервис версий."""
        self._transaction = dependencies.transaction
        self._versions = dependencies.version_catalog

    def service_versions(self, service_id: str, limit: int, before: str) -> dict[str, Any]:
        """Версии конфигураций всех экземпляров сервиса, новые первыми, без тел конфигураций."""
        with self._transaction():
            # Читаем страницу версий сервиса (неизвестный сервис — ошибка).
            page = self._versions.service_page(service_id, limit, before)
        # Собираем ответ.
        response = page_to_response(page)
        return response

    def instance_versions(self, source_id: str, instance_id: str, limit: int, before: str) -> dict[str, Any]:
        """Версии конфигураций одного экземпляра."""
        with self._transaction():
            # Читаем страницу версий экземпляра (неизвестный экземпляр — ошибка).
            page = self._versions.instance_page(source_id, instance_id, limit, before)
        # Собираем ответ.
        response = page_to_response(page)
        return response

    def version(self, version_id: int) -> dict[str, Any]:
        """Одна версия с телом конфигурации."""
        with self._transaction():
            # Читаем версию (отсутствующая — ошибка).
            version = self._versions.version(version_id)
        # Собираем ответ.
        response = version_to_response(version)
        return response

    def compare(self, older_id: int, newer_id: int) -> dict[str, Any]:
        """Изменения от одной версии к другой."""
        with self._transaction():
            # Читаем обе версии и сравниваем их конфигурации.
            comparison = self._versions.compare(older_id, newer_id)
        # Собираем ответ.
        response = comparison_to_response(comparison)
        return response
