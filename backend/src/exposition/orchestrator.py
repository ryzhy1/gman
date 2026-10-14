"""Оркестратор модуля exposition: состояние inventory как метрики Prometheus для правил алертов.

Отдается на /metrics контейнера API. Его опрашивает Prometheus внутри сети Compose; nginx этот путь
не проксирует (только /api/ и /grafana/), поэтому снаружи сервера он недоступен.
"""
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone

from exposition.services.inventory_samples import InventorySamples
from exposition.services.inventory_state import InventoryState
from exposition.services.text_format import TextFormat
from exposition.services.tls_samples import TlsSamples

Transaction = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True)
class ExpositionDependencies:
    """Готовые зависимости оркестратора exposition."""

    transaction: Transaction
    inventory_state: InventoryState
    inventory_samples: InventorySamples
    tls_samples: TlsSamples
    text_format: TextFormat


class ExpositionOrchestrator:
    """Единственная точка входа модуля exposition."""

    def __init__(self, dependencies: ExpositionDependencies) -> None:
        """Запоминает сервисы модуля."""
        self._transaction = dependencies.transaction
        self._state = dependencies.inventory_state
        self._inventory_samples = dependencies.inventory_samples
        self._tls_samples = dependencies.tls_samples
        self._text_format = dependencies.text_format

    def render(self) -> str:
        """Все метрики в текстовом формате Prometheus."""
        now = datetime.now(timezone.utc)
        with self._transaction():
            # Читаем сохраненное состояние: источники, экземпляры, Events узлов, последние релизы.
            state = self._state.read()
        # Превращаем состояние inventory в выборки.
        samples = self._inventory_samples.sources(state.sources, now)
        samples += self._inventory_samples.instances(state.instances)
        samples += self._inventory_samples.node_events(state.node_event_times)
        samples += self._inventory_samples.releases(state.releases)
        # Добавляем сроки наблюдаемых TLS-файлов.
        samples += self._tls_samples.samples()
        # Выводим в текстовом формате.
        text = self._text_format.render(samples)
        return text
