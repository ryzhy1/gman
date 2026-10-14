"""Контейнер и pod процесса по /proc/<pid>/cgroup (v1 и v2, драйверы systemd и cgroupfs)."""
import re
from pathlib import Path

from collection.entity import POD_UID, ProcessOwner

# cgroup контейнера — 64-символьный hex-сегмент, голый (cgroupfs) или с префиксом runtime и .scope (systemd):
# docker-, cri-containerd-, crio-, libpod-. crio-conmon-<id> — процесс-монитор CRI-O, не контейнер.
CONTAINER_SEGMENT = re.compile(r"(?:([a-z][a-z0-9-]*)-)?([0-9a-f]{64})(?:\.scope)?")
# ":" разделяет форму runc "slice:prefix:id", которая встречается при разных драйверах cgroup kubelet и runtime.
SEGMENT_SEPARATOR = re.compile(r"[/:]")
LINE_PARTS = 3
CONMON = "conmon"
SYSTEMD_UID_SEPARATOR = "_"
UID_SEPARATOR = "-"


class ProcCgroups:
    """Владельцы процессов хоста; в контейнере нужен hostPID (nvidia-smi сообщает PID своего пространства)."""

    def __init__(self, proc_root: Path) -> None:
        """Запоминает /proc хоста."""
        self._proc_root = proc_root

    def owner(self, pid: int) -> ProcessOwner:
        """Владелец процесса; завершившийся процесс или чужой /proc — пустой владелец."""
        try:
            text = (self._proc_root / str(pid) / "cgroup").read_text()
        except OSError:
            empty = ProcessOwner()
            return empty
        owner = self.parse(text)
        return owner

    def container_of(self, pid: int) -> str:
        """ID контейнера Docker/containerd/CRI-O процесса; у процессов хоста его нет."""
        container_id = self.owner(pid).container_id
        return container_id

    @staticmethod
    def parse(text: str) -> ProcessOwner:
        """Контейнер и pod из содержимого файла cgroup; пусто для процессов хоста."""
        found = ProcessOwner()
        for line in text.splitlines():
            parts = line.split(":", LINE_PARTS - 1)
            if len(parts) != LINE_PARTS:
                continue
            current = _line_owner(parts[2])
            if current.container_id:
                # Иерархии v1 согласованы между собой: достаточно первой полной.
                return current
            if current.pod_uid and not found.pod_uid:
                # Только cgroup уровня pod-а (например, conmon или sandbox pause).
                found = current
        return found


def _line_owner(path: str) -> ProcessOwner:
    """Владелец по пути cgroup одной строки; побеждает самый внутренний сегмент контейнера."""
    container_id, pod_uid = "", ""
    for segment in SEGMENT_SEPARATOR.split(path):
        match = CONTAINER_SEGMENT.fullmatch(segment)
        if match and CONMON not in (match[1] or ""):
            container_id = match[2]
        uid = POD_UID.search(segment)
        if uid:
            pod_uid = uid[1].replace(SYSTEMD_UID_SEPARATOR, UID_SEPARATOR)
    owner = ProcessOwner(container_id=container_id, pod_uid=pod_uid)
    return owner
