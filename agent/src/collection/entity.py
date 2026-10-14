"""Сущности модуля collection: документы снимка и их части, владельцы процессов GPU, ссылки на pod-ы.

Агент отдает в API JSON-документ снимка; поля, добавленные в API, проходят без изменений агента. Поэтому
экземпляр, GPU и снимок — документы, обернутые сущностями с типизированным доступом к полям, которые
использует логика агента.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]
AGENT_VERSION = "0.8.0"
DOCKER = "docker"
KUBERNETES = "kubernetes"
KUBERNETES_NODE = "kubernetes-node"
# UID pod-а в cgroup kubepods и в сообщениях OOM: дефисы у драйвера cgroupfs, подчеркивания в именах slice
# systemd; у статических pod-ов — 32-символьный хеш конфигурации без разделителей.
POD_UID = re.compile(r"pod([0-9a-f]{8}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{12})(?![0-9a-f])")
RFC3339 = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")
MICROSECOND_DIGITS = 6
UTC_OFFSET = "+00:00"
UTC_SUFFIX = "Z"
FIRST_YEAR = 1


class DaemonInspectError(RuntimeError):
    """docker inspect не работает для существующих контейнеров: отказ демона, а не удаление контейнера."""


class RuntimeTimestamp:
    """Время RFC 3339 от Docker и Kubernetes (наносекунды, "Z")."""

    @staticmethod
    def parse(value: str | None) -> datetime | None:
        """Aware datetime; отсутствующее, неразборчивое или «никогда» (год 1) — None."""
        match = RFC3339.fullmatch(value or "")
        if not match:
            return None
        fraction = (match[2] or "0")[:MICROSECOND_DIGITS].ljust(MICROSECOND_DIGITS, "0")
        zone = UTC_OFFSET if match[3] == UTC_SUFFIX else match[3]
        try:
            parsed = datetime.fromisoformat(match[1] + "." + fraction + zone)
        except ValueError:
            return None
        if parsed.year <= FIRST_YEAR:
            return None
        return parsed

    @staticmethod
    def newest(*values: str | None) -> str | None:
        """Самое позднее из времен в исходном виде (первое из равных); None, если времен нет."""
        newest_value, newest_time = None, None
        for value in values:
            moment = RuntimeTimestamp.parse(value)
            if moment is not None and (newest_time is None or moment > newest_time):
                newest_value, newest_time = value, moment
        return newest_value


@dataclass
class Instance:
    """Экземпляр снимка: контейнер Docker или контейнер pod-а."""

    document: JsonObject

    @property
    def id(self) -> str:
        """ID экземпляра в источнике."""
        instance_id = self.document["id"]
        return instance_id

    @property
    def last_oom_at(self) -> str | None:
        """Время последнего OOM-kill."""
        moment = self.document.get("last_oom_at")
        return moment

    def record_oom(self, at: str) -> None:
        """Запоминает OOM-kill, если он новее известного."""
        self.document["last_oom_at"] = RuntimeTimestamp.newest(self.last_oom_at, at)


@dataclass(frozen=True)
class ProcessOwner:
    """Контейнер и pod процесса по его cgroup; пусто для процессов хоста."""

    container_id: str = ""
    pod_uid: str = ""


@dataclass(frozen=True)
class PodContainer:
    """Pod (и контейнер, если известен) — владелец процесса GPU на узле Kubernetes."""

    pod_uid: str
    namespace: str
    pod: str
    container: str = ""
    instance_id: str = ""

    def attribution(self) -> JsonObject:
        """Поля атрибуции процесса; контейнер и ID экземпляра — только когда процесс отнесен к контейнеру."""
        fields = {"pod_uid": self.pod_uid, "namespace": self.namespace, "pod": self.pod}
        if self.container:
            fields.update(container=self.container, instance_id=self.instance_id)
        return fields


@dataclass(frozen=True)
class PodIndex:
    """Владельцы процессов узла: по ID контейнера runtime и по UID pod-а из cgroup."""

    by_container: dict[str, PodContainer]
    by_pod: dict[str, PodContainer]


@dataclass
class GpuProcess:
    """Процесс GPU и его владелец."""

    document: JsonObject

    @property
    def container_id(self) -> str:
        """ID контейнера runtime (пусто для процессов хоста)."""
        container_id = self.document.get("container_id", "")
        return container_id

    @property
    def pod_uid(self) -> str:
        """UID pod-а из cgroup (пусто вне Kubernetes)."""
        pod_uid = self.document.get("pod_uid", "")
        return pod_uid

    def attribute(self, owner: PodContainer) -> None:
        """Добавляет pod, контейнер и ID экземпляра inventory ("<pod uid>:<container>")."""
        self.document.update(owner.attribution())


@dataclass
class Gpu:
    """GPU снимка."""

    document: JsonObject

    @property
    def id(self) -> str:
        """UUID GPU (NVIDIA) или PCI-адрес (AMD)."""
        gpu_id = self.document["id"]
        return gpu_id

    @property
    def processes(self) -> list[GpuProcess]:
        """Процессы GPU."""
        processes = [GpuProcess(document) for document in self.document.get("processes", [])]
        return processes

    def add_process(self, process: JsonObject) -> None:
        """Добавляет процесс GPU."""
        self.document.setdefault("processes", []).append(process)


@dataclass(frozen=True)
class ComputeApp:
    """Процесс из nvidia-smi --query-compute-apps."""

    gpu_uuid: str
    pid: int
    used_memory_bytes: int | None


@dataclass(frozen=True)
class PodRef:
    """Что нужно для сопоставления Events с pod-ом: идентичность, сервис, узел и контейнеры."""

    uid: str
    namespace: str
    name: str
    service_id: str
    environment: str
    node: str
    containers: frozenset[str]
    # ID контейнера runtime -> имя контейнера.
    container_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectionTarget:
    """Что собирает агент: runtime, ID источника, для GPU-узла — узел и кластер, отметка OOM Docker."""

    runtime: str
    source_id: str
    node: str = ""
    cluster: str = ""
    oom_mark: Path | None = None


@dataclass(frozen=True)
class KubernetesState:
    """Объекты кластера для снимка: pod-ы, узлы и сырые Events (их сопоставляет маппер)."""

    pods: list[JsonObject]
    nodes: list[JsonObject]
    events: list[JsonObject] | None
