"""Debounce по `docker events`, захват короткоживущих контейнеров и время OOM-kill."""
from collections.abc import Callable
from datetime import datetime, timezone

from collection.entity import Instance, RuntimeTimestamp
from runtime_access.docker_cli import ContainerEvent
from watching.entity import JsonObject, SnapshotDocument, WatchTimings
from watching.ports import SnapshotSource
from watching.services.debounced_watcher import DebouncedWatcher
from watching.services.ephemeral_snapshots import with_ephemeral

CONTAINER = "container"
# События, меняющие снимок. exec_* (healthcheck), attach, resize, top и подобные игнорируются: иначе снимок
# собирался бы на каждый интервал healthcheck.
WATCH_TRIGGERS = {"create", "start", "restart", "die", "oom", "kill", "stop", "pause", "unpause", "update", "rename",
                  "destroy", "health_status"}
# На этих событиях контейнер осматривается сразу: к следующему снимку его может уже не быть.
WATCH_CAPTURE = {"start", "die"}
OOM = "oom"

Capture = Callable[[ContainerEvent, JsonObject | None], JsonObject]


class DockerWatcher(DebouncedWatcher):
    """Снимки по событиям контейнеров Docker."""

    def __init__(self, now: float, timings: WatchTimings, capture: Capture) -> None:
        """Запоминает тайминги и захват состояния контейнера (inspect, а без него — по событиям)."""
        super().__init__(now, timings)
        self._capture = capture
        # ID контейнера -> время последнего события `oom`. Docker сбрасывает State.OOMKilled при перезапуске
        # по политике, а OOM-kill дочернего процесса не останавливает контейнер вовсе.
        self._ooms: dict[str, str] = {}

    @property
    def ooms(self) -> dict[str, str]:
        """Известные времена OOM-kill по ID контейнера."""
        ooms = self._ooms
        return ooms

    def collect(self, source: SnapshotSource) -> JsonObject:
        """Полный снимок хоста."""
        snapshot = source.snapshot()
        return snapshot

    def handle(self, item: object, now: float) -> None:
        """Событие контейнера: OOM запоминается, start/die осматриваются, значимое событие запускает отсчет."""
        event: ContainerEvent = item
        if event.type != CONTAINER or event.action not in WATCH_TRIGGERS:
            return
        if event.action == OOM and event.container_id:
            moment = event.time or datetime.now(timezone.utc).isoformat()
            self._ooms[event.container_id] = RuntimeTimestamp.newest(self._ooms.get(event.container_id), moment)
        if event.action in WATCH_CAPTURE and event.container_id:
            self._seen[event.container_id] = self._capture(event, self._seen.get(event.container_id))
        self.trigger(now)

    def snapshots(self, snapshot: SnapshotDocument, seen: dict[str, JsonObject]) -> list[SnapshotDocument]:
        """Снимки одного сбора: эфемерные контейнеры и времена OOM добавлены."""
        result = with_ephemeral(snapshot, seen)
        if not self._ooms:
            return result
        for instance in _unique_instances(result):
            if instance.id in self._ooms:
                instance.record_oom(self._ooms[instance.id])
        # Сообщается, пока контейнер существует; после записи API хранит последнее время сам.
        present = snapshot.instance_ids
        self._ooms = {container_id: at for container_id, at in self._ooms.items() if container_id in present}
        return result


def _unique_instances(snapshots: list[SnapshotDocument]) -> list[Instance]:
    """Экземпляры снимков без повторов: промежуточный снимок делит документы с настоящим."""
    unique: dict[int, Instance] = {}
    for snapshot in snapshots:
        for instance in snapshot.instances:
            unique[id(instance.document)] = instance
    instances = list(unique.values())
    return instances
