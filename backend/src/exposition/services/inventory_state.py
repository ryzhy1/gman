"""Сохраненное состояние inventory, нужное для метрик, одним чтением."""
from dataclasses import dataclass

from exposition.adapters.inventory_state_repository import InventoryStateRepository
from exposition.entity import InstanceReading, NodeEventTime, ReleaseReading, SourceReading


@dataclass(frozen=True)
class InventoryStateView:
    """Источники, экземпляры, времена Events узлов и последние релизы."""

    sources: list[SourceReading]
    instances: list[InstanceReading]
    node_event_times: list[NodeEventTime]
    releases: list[ReleaseReading]


class InventoryState:
    """Читает все части состояния в рамках одной транзакции."""

    def __init__(self, repository: InventoryStateRepository) -> None:
        """Запоминает репозиторий состояния."""
        self._repository = repository

    def read(self) -> InventoryStateView:
        """Текущее состояние inventory."""
        view = InventoryStateView(sources=self._repository.sources(), instances=self._repository.instances(),
                                  node_event_times=self._repository.node_event_times(),
                                  releases=self._repository.latest_releases())
        return view
