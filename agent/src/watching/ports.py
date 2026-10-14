"""Протокол модуля collection, которым пользуется режим --watch (реализует оркестратор collection)."""
from typing import Protocol

from collection.entity import JsonObject, PodRef
from runtime_access.docker_cli import ContainerEvent


class SnapshotSource(Protocol):
    """Снимки и разбор объектов runtime."""

    def snapshot(self) -> JsonObject:
        """Полный снимок настроенного runtime."""

    def cluster_snapshot(self, pods: list[JsonObject] | None = None, with_events: bool = True) -> JsonObject:
        """Снимок кластера из pod-ов кеша watch (Events добавляет watch)."""

    def capture_container(self, event: ContainerEvent, previous: JsonObject | None) -> JsonObject:
        """Состояние контейнера на момент события."""

    def pod_instances(self, pod: JsonObject) -> list[JsonObject]:
        """Экземпляры pod-а."""

    def pod_ref(self, pod: JsonObject) -> PodRef:
        """Ссылка на pod для сопоставления Events."""

    def map_events(self, events: list[JsonObject], refs: dict[str, PodRef]) -> list[JsonObject]:
        """Разрешенные Events известных pod-ов и узлов."""

    def relevant_event(self, event: JsonObject) -> bool:
        """Запускает ли Event снимок."""
