"""Сущности модуля watching: тайминги debounce и элементы потока watch Kubernetes."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from collection.entity import Instance

JsonObject = dict[str, Any]
PODS = "pods"
EVENTS = "events"
NODE_EVENTS = "node-events"
SYNC = "SYNC"
ADDED = "ADDED"
MODIFIED = "MODIFIED"
DELETED = "DELETED"
CHANGES = (ADDED, MODIFIED, DELETED)


@dataclass(frozen=True)
class WatchTimings:
    """Снимок — через debounce секунд тишины, не позже max_delay после первого события и не реже interval."""

    debounce: float = 2.0
    max_delay: float = 10.0
    interval: float = 60.0
    # Сколько помнить удаленные pod-ы, чтобы сопоставлять их поздние Events.
    keep_deleted: float = 600.0
    # Через сколько повторить неудавшийся сбор.
    retry_delay: float = 10.0


@dataclass(frozen=True)
class KubeObject:
    """Объект потока watch: UID и сырой JSON (его разбирает маппер модуля collection)."""

    uid: str
    raw: JsonObject


@dataclass(frozen=True)
class WatchItem:
    """Элемент потока watch: SYNC после каждого list (все объекты) или ADDED/MODIFIED/DELETED (один объект)."""

    resource: str
    kind: str
    objects: list[KubeObject] = field(default_factory=list)


@dataclass(frozen=True)
class SnapshotDocument:
    """Снимок, собранный модулем collection, с операциями, которые нужны режиму --watch."""

    document: JsonObject

    @property
    def instances(self) -> list[Instance]:
        """Экземпляры снимка (изменения видны в документе)."""
        instances = [Instance(item) for item in self.document["instances"]]
        return instances

    @property
    def instance_ids(self) -> set[str]:
        """ID экземпляров снимка."""
        ids = {item["id"] for item in self.document["instances"]}
        return ids

    def with_instances(self, extra: list[JsonObject]) -> "SnapshotDocument":
        """Копия снимка с дополнительными экземплярами."""
        extended = SnapshotDocument(dict(self.document, instances=self.document["instances"] + extra))
        return extended

    def shifted(self, delta: timedelta) -> "SnapshotDocument":
        """Копия снимка с временем наблюдения позже на delta."""
        observed = datetime.fromisoformat(self.document["observed_at"]) + delta
        moved = SnapshotDocument(dict(self.document, observed_at=observed.isoformat()))
        return moved

    def with_events(self, events: list[JsonObject]) -> "SnapshotDocument":
        """Копия снимка с Events Kubernetes."""
        updated = SnapshotDocument(dict(self.document, events=events))
        return updated
