"""Debounce по watch Kubernetes (pod-ы, Events pod-ов и узлов) с кешем pod-ов из того же потока.

Снимки строятся из кеша (узлы по-прежнему читаются на каждый снимок). Pod-ы, увиденные в окне, хранятся
с последним состоянием, поэтому pod, созданный и удаленный между снимками, все равно попадает в отчет.
Удаленные pod-ы помнятся keep_deleted секунд, чтобы сопоставлять их поздние Events.
"""
from collection.entity import Instance, PodRef
from watching.entity import (CHANGES, DELETED, EVENTS, NODE_EVENTS, PODS, SYNC, JsonObject, KubeObject,
                             SnapshotDocument, WatchItem, WatchTimings)
from watching.ports import SnapshotSource
from watching.services.debounced_watcher import DebouncedWatcher
from watching.services.ephemeral_snapshots import with_ephemeral

EVENT_RESOURCES = (EVENTS, NODE_EVENTS)
EVENT_KINDS = (SYNC, "ADDED", "MODIFIED")


class KubeWatcher(DebouncedWatcher):
    """Снимки кластера по потоку watch."""

    def __init__(self, now: float, timings: WatchTimings, source: SnapshotSource) -> None:
        """Запоминает тайминги и разбор объектов модулем collection."""
        super().__init__(now, timings)
        self._source = source
        self._pods: dict[str, JsonObject] = {}
        self._deleted: dict[str, tuple[PodRef, float]] = {}
        self._events: dict[str, JsonObject] = {}
        self._synced = False
        self._now = now

    def collect(self, source: SnapshotSource) -> JsonObject:
        """Снимок кластера из кеша pod-ов; Events добавляет сам watch."""
        snapshot = source.cluster_snapshot(pods=self.pod_list(), with_events=False)
        return snapshot

    def pod_list(self) -> list[JsonObject] | None:
        """Pod-ы из кеша или None до первого list (тогда снимок читает их сам)."""
        if not self._synced:
            return None
        pods = list(self._pods.values())
        return pods

    def handle(self, item: object, now: float) -> None:
        """Элемент потока watch; изменения pod-ов и разрешенные Events запускают отсчет debounce."""
        change: WatchItem = item
        self._now = now
        if change.resource == PODS:
            changed = self._pods_changed(change, now)
        elif change.resource in EVENT_RESOURCES and change.kind in EVENT_KINDS:
            changed = self._events_changed(change)
        else:
            changed = False
        if changed:
            self.trigger(now)

    def snapshots(self, snapshot: SnapshotDocument, seen: dict[str, JsonObject]) -> list[SnapshotDocument]:
        """Снимки одного сбора; Events, не сопоставленные известному pod-у, отбрасываются (relist их вернет)."""
        keep = self._timings.keep_deleted
        self._deleted = {uid: entry for uid, entry in self._deleted.items() if self._now - entry[1] <= keep}
        refs = {uid: ref for uid, (ref, _) in self._deleted.items()}
        for uid, pod in self._pods.items():
            refs[uid] = self._source.pod_ref(pod)
        events = self._source.map_events(list(self._events.values()), refs)
        self._events = {}
        result = [item.with_events([]) for item in with_ephemeral(snapshot, seen)]
        result[-1] = result[-1].with_events(events)
        return result

    def _pods_changed(self, change: WatchItem, now: float) -> bool:
        """Список (SYNC) заменяет кеш; изменение pod-а обновляет кеш и запоминает его экземпляры."""
        if change.kind == SYNC:
            self._resync_pods(change.objects, now)
            return True
        if change.kind not in CHANGES:
            return False
        pod = change.objects[0]
        if change.kind == DELETED:
            self._pods.pop(pod.uid, None)
            self._deleted[pod.uid] = (self._source.pod_ref(pod.raw), now)
        else:
            self._pods[pod.uid] = pod.raw
        for document in self._source.pod_instances(pod.raw):
            self._seen[Instance(document).id] = document
        return True

    def _resync_pods(self, objects: list[KubeObject], now: float) -> None:
        """Новый список pod-ов; удаленные, пока watch не работал, остаются известны для их Events."""
        fresh = {pod.uid: pod.raw for pod in objects}
        for uid, pod in self._pods.items():
            if uid not in fresh:
                self._deleted[uid] = (self._source.pod_ref(pod), now)
        self._pods = fresh
        self._synced = True

    def _events_changed(self, change: WatchItem) -> bool:
        """Запоминает разрешенные Events; только они запускают снимок."""
        relevant = [event for event in change.objects if self._source.relevant_event(event.raw)]
        for event in relevant:
            self._events[event.uid] = event.raw
        changed = bool(relevant)
        return changed
