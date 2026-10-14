"""HTTP-модели снимка inventory, который присылает агент (agent/src)."""
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .validators import Strict, not_future

SOURCE_ID_PATTERN = r"^[a-zA-Z0-9_.-]{1,100}$"
CLUSTER_PATTERN = r"^[a-zA-Z0-9_.-]{0,100}$"
EVENT_UID_PATTERN = r"^[a-zA-Z0-9_.:-]{1,100}$"
# Имя узла Kubernetes (DNS subdomain): становится значением метки метрик.
NODE_NAME_PATTERN = r"^([a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?)?$"
PSTATE_TEXT = 10
MODE_TEXT = 50
SHORT_TEXT = 100
MEDIUM_TEXT = 200
NAME_TEXT = 300
REASON_TEXT = 500
CONTROLLER_TEXT = 600
INSTANCE_REF_TEXT = 700
IMAGE_TEXT = 1000
LONG_TEXT = 2000
PERCENT_MAX = 100
SMALL_LIST = 10
TAG_LIST = 20
TAINT_LIST = 50
ITEM_LIST = 100
MOUNT_LIST = 200
GPU_LIST = 128
NODE_LIST = 1000
ENV_LIST = 2000
INSTANCE_LIST = 10000
ERROR_LIST = 50
ERROR_TEXT = 500
UNKNOWN = "unknown"
KUBERNETES_NODE = "kubernetes-node"
NODE_KIND = "Node"
SOURCE_ID_SEPARATOR = "."

# Те же списки, что KUBE_EVENT_REASONS и NODE_EVENT_REASONS агента; остальное отклоняется, а не сохраняется.
KUBE_EVENT_REASONS = {"OOMKilling", "BackOff", "FailedScheduling", "Unhealthy", "Killing", "Failed", "Evicted",
                      "FailedMount", "FailedCreatePodSandBox", "Preempting"}
NODE_EVENT_REASONS = {"OOMKilling", "SystemOOM", "NodeNotReady", "NodeHasDiskPressure", "NodeHasInsufficientMemory",
                      "NodeHasInsufficientPID", "EvictionThresholdMet", "Rebooted", "KernelOops", "TaskHung",
                      "FilesystemIsReadOnly", "ImageGCFailed"}

Strings = list[str]


def _text(limit: int, default: str = "") -> Any:
    """Описание строкового поля с ограничением длины (pydantic Field типизирован как Any)."""
    field = Field(default=default, max_length=limit)
    return field


def _strings(limit: int) -> Any:
    """Список строк с ограничением числа элементов."""
    field = Field(default_factory=list, max_length=limit)
    return field


def _non_negative() -> Any:
    """Необязательное неотрицательное число."""
    field = Field(default=None, ge=0)
    return field


def _percent() -> Any:
    """Необязательный процент 0..100."""
    field = Field(default=None, ge=0, le=PERCENT_MAX)
    return field


class Config(Strict):
    """Конфигурация запуска экземпляра."""

    image: str = Field(max_length=IMAGE_TEXT)
    image_id: str = _text(IMAGE_TEXT)
    cpu_limit: float | None = _non_negative()
    cpu_request: float | None = _non_negative()
    memory_limit_bytes: int | None = _non_negative()
    memory_request_bytes: int | None = _non_negative()
    cpuset: str = _text(MEDIUM_TEXT)
    restart_policy: str = _text(MEDIUM_TEXT)
    ports: Strings = _strings(ITEM_LIST)
    mounts: Strings = _strings(MOUNT_LIST)
    networks: Strings = _strings(ITEM_LIST)
    network_mode: str = _text(NAME_TEXT)
    pid_mode: str = _text(NAME_TEXT)
    ipc_mode: str = _text(NAME_TEXT)
    user: str = _text(NAME_TEXT)
    # Окружение как "NAME=value", показывается как есть (значения могут быть секретами).
    env: Strings = _strings(ENV_LIST)
    # Docker: dotenv-файлы рабочего каталога, которые приложение читает само, {путь: ["NAME=value"]}.
    env_files: dict[str, Strings] = Field(default_factory=dict, max_length=TAG_LIST)
    # Параметры запуска как есть (аргументы могут быть секретами). Docker: ENTRYPOINT и CMD; Kubernetes:
    # переопределения command/args pod-а, пусто — у образа свои. None — агент до 0.8.
    command: Strings | None = Field(default=None, max_length=IMAGE_TEXT)
    args: Strings | None = Field(default=None, max_length=IMAGE_TEXT)
    cap_add: Strings = _strings(ITEM_LIST)
    cap_drop: Strings = _strings(ITEM_LIST)
    shm_size_bytes: int | None = _non_negative()
    ulimits: Strings = _strings(ITEM_LIST)
    log_driver: str = _text(SHORT_TEXT)
    probes: Strings = _strings(SMALL_LIST)
    runtime: str = _text(SHORT_TEXT)
    gpu_requests: Strings = _strings(ITEM_LIST)
    privileged: bool = False
    readonly_rootfs: bool = False


class Origin(Strict):
    """Разрешенные подсказки о происхождении; ни одна не доказывает, кто запустил экземпляр.

    started_by — метка gman.started-by: имя, которое указал сам запускающий (без проверки).
    """

    compose_project: str = _text(LONG_TEXT)
    compose_service: str = _text(LONG_TEXT)
    compose_working_dir: str = _text(LONG_TEXT)
    image_source: str = _text(LONG_TEXT)
    image_revision: str = _text(LONG_TEXT)
    image_version: str = _text(LONG_TEXT)
    image_created: str = _text(LONG_TEXT)
    app_name: str = _text(LONG_TEXT)
    app_version: str = _text(LONG_TEXT)
    managed_by: str = _text(LONG_TEXT)
    helm_chart: str = _text(LONG_TEXT)
    field_managers: str = _text(LONG_TEXT)
    started_by: str = _text(LONG_TEXT)


class Instance(Strict):
    """Экземпляр: контейнер Docker или контейнер pod-а."""

    id: str = Field(max_length=NAME_TEXT)
    name: str = Field(max_length=NAME_TEXT)
    service_id: str = _text(SHORT_TEXT)
    environment: str = _text(SHORT_TEXT, UNKNOWN)
    runtime: Literal["docker", "kubernetes"]
    node: str = _text(NAME_TEXT)
    namespace: str = _text(NAME_TEXT)
    controller: str = _text(CONTROLLER_TEXT)
    status: str = Field(max_length=SHORT_TEXT)
    health: str = _text(SHORT_TEXT)
    ready: bool | None = None
    reason: str = _text(REASON_TEXT)
    exit_code: int | None = None
    last_termination_reason: str = _text(REASON_TEXT)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    restarts: int = Field(default=0, ge=0)
    # Последнее OOM: событие Docker `oom` (--watch) или OOMKilled; Kubernetes — последнее/текущее завершение.
    # API хранит его после первого наблюдения: Docker сбрасывает OOMKilled при перезапуске контейнера.
    last_oom_at: datetime | None = None
    origin: Origin = Field(default_factory=Origin)
    config: Config


class GPUProcess(Strict):
    """Процесс GPU и его владелец: Docker — по container_id; pod Kubernetes — еще по pod и instance_id."""

    pid: int = Field(ge=0)
    container_id: str = _text(SHORT_TEXT)
    used_memory_bytes: int | None = _non_negative()
    pod_uid: str = _text(SHORT_TEXT)
    namespace: str = _text(NAME_TEXT)
    pod: str = _text(NAME_TEXT)
    container: str = _text(NAME_TEXT)
    # ID экземпляра Kubernetes "<pod uid>:<container name>"; пусто, если процесс не отнесен к контейнеру.
    instance_id: str = _text(INSTANCE_REF_TEXT)


class GPU(Strict):
    """Неподдерживаемые показания — null, а не ноль; температуры — по датчикам."""

    id: str = Field(max_length=NAME_TEXT)
    vendor: str = Field(max_length=SHORT_TEXT)
    model: str = _text(NAME_TEXT)
    pci_bus_id: str = _text(SHORT_TEXT)
    driver_version: str | None = Field(default=None, max_length=SHORT_TEXT)
    pstate: str | None = Field(default=None, max_length=PSTATE_TEXT)
    utilization_percent: float | None = _percent()
    memory_utilization_percent: float | None = _percent()
    memory_total_bytes: int | None = _non_negative()
    memory_used_bytes: int | None = _non_negative()
    temperature_c: float | None = None
    hotspot_temperature_c: float | None = None
    memory_temperature_c: float | None = None
    power_w: float | None = _non_negative()
    power_limit_w: float | None = _non_negative()
    sm_clock_mhz: float | None = _non_negative()
    memory_clock_mhz: float | None = _non_negative()
    max_sm_clock_mhz: float | None = _non_negative()
    fan_percent: float | None = _percent()
    fan_rpm: float | None = _non_negative()
    throttle_reasons: list[str] = _strings(TAG_LIST)
    ecc_uncorrected_errors: int | None = _non_negative()
    pcie_generation: int | None = _non_negative()
    pcie_width: int | None = _non_negative()
    mig_mode: str | None = Field(default=None, max_length=MODE_TEXT)
    persistence_mode: str | None = Field(default=None, max_length=MODE_TEXT)
    processes: list[GPUProcess] = Field(default_factory=list, max_length=NODE_LIST)


class Node(Strict):
    """Узел Kubernetes: емкость и запросы pod-ов, размещенных на нем."""

    name: str = Field(max_length=NAME_TEXT)
    internal_ip: str = _text(SHORT_TEXT)
    ready: bool
    unschedulable: bool = False
    pressure: list[str] = _strings(TAG_LIST)
    roles: list[str] = _strings(TAG_LIST)
    taints: list[str] = _strings(TAINT_LIST)
    kubelet_version: str = _text(SHORT_TEXT)
    os: str = _text(NAME_TEXT)
    kernel: str = _text(NAME_TEXT)
    runtime_version: str = _text(NAME_TEXT)
    cpu_capacity: float | None = _non_negative()
    cpu_allocatable: float | None = _non_negative()
    cpu_requested: float | None = _non_negative()
    memory_capacity_bytes: int | None = _non_negative()
    memory_allocatable_bytes: int | None = _non_negative()
    memory_requested_bytes: int | None = _non_negative()
    gpu_capacity: int = Field(default=0, ge=0)
    gpu_allocatable: int = Field(default=0, ge=0)
    gpu_requested: int = Field(default=0, ge=0)
    gpu_product: str = _text(NAME_TEXT)
    gpu_memory_mib: int | None = _non_negative()
    created_at: datetime | None = None


class RuntimeEvent(Strict):
    """Event Kubernetes pod-а или узла без свободного текста сообщения.

    Event узла несет pod и сервис, только если агент сопоставил OOM с pod-ом.
    """

    uid: str = Field(pattern=EVENT_UID_PATTERN)
    kind: Literal["Pod", "Node"] = "Pod"
    # Обязателен для Events узлов; становится значением метки метрик.
    node: str = Field(default="", pattern=NODE_NAME_PATTERN)
    reason: str = Field(max_length=SHORT_TEXT)
    type: Literal["Normal", "Warning"] = "Warning"
    count: int = Field(default=1, ge=1)
    first_at: datetime | None = None
    last_at: datetime
    service_id: str = _text(SHORT_TEXT)
    environment: str = _text(SHORT_TEXT, UNKNOWN)
    namespace: str = _text(NAME_TEXT)
    pod: str = _text(NAME_TEXT)
    container: str = _text(NAME_TEXT)
    instance_id: str = _text(INSTANCE_REF_TEXT)
    instance_name: str = _text(INSTANCE_REF_TEXT)
    component: str = _text(SHORT_TEXT)
    detail: str = _text(NAME_TEXT)

    _time = field_validator("last_at")(not_future)

    @model_validator(mode="after")
    def _allowlisted(self) -> "RuntimeEvent":
        """Причина из списка своего вида; Event узла называет свой узел."""
        allowed = NODE_EVENT_REASONS if self.kind == NODE_KIND else KUBE_EVENT_REASONS
        if self.reason not in allowed:
            raise ValueError("Event reason is not allowlisted")
        if self.kind == NODE_KIND and not self.node:
            raise ValueError("Node Events name their node")
        return self


class Snapshot(Strict):
    """Снимок Docker-хоста, кластера Kubernetes или GPU-узла кластера."""

    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    observed_at: datetime
    # kubernetes-node: GPU одного узла кластера; pod-ы остаются у источника-кластера.
    source_type: Literal["docker-host", "kubernetes-cluster", "kubernetes-node"]
    # Только kubernetes-node: source_id источника-кластера, которому принадлежит узел.
    cluster: str = Field(default="", pattern=CLUSTER_PATTERN)
    agent_version: str = _text(MODE_TEXT)
    hostname: str = Field(max_length=NAME_TEXT)
    # docker-host: адрес хоста, показывается рядом с его сервисами.
    ip: str = _text(SHORT_TEXT)
    os: str = _text(NAME_TEXT)
    kernel: str = _text(NAME_TEXT)
    cpu_model: str = _text(NAME_TEXT)
    runtime_version: str = _text(NAME_TEXT)
    uptime_seconds: int | None = _non_negative()
    cpu_count: int | None = Field(default=None, ge=1)
    memory_total_bytes: int | None = _non_negative()
    gpus: list[GPU] = Field(default_factory=list, max_length=GPU_LIST)
    nodes: list[Node] = Field(default_factory=list, max_length=NODE_LIST)
    instances: list[Instance] = Field(default_factory=list, max_length=INSTANCE_LIST)
    errors: list[str] = _strings(ERROR_LIST)
    # kubernetes-cluster: Events pod-ов и узлов с прошлого отчета агента; повторы API отсекает само.
    events: list[RuntimeEvent] = Field(default_factory=list, max_length=NODE_LIST)

    _time = field_validator("observed_at")(not_future)

    @field_validator("errors")
    @classmethod
    def _short_errors(cls, value: list[str]) -> list[str]:
        """Ошибки сбора укорачиваются: это сводка, а не журнал."""
        shortened = [error[:ERROR_TEXT] for error in value]
        return shortened

    @model_validator(mode="after")
    def _node_snapshot(self) -> "Snapshot":
        """Снимок узла описывает только узел, названный в его source_id, и не несет экземпляров."""
        if self.source_type != KUBERNETES_NODE:
            return self
        # Экземпляры узла присылает источник-кластер: повтор задвоил бы историю.
        if self.instances or not self.cluster:
            raise ValueError("kubernetes-node snapshots carry a cluster and no instances")
        # Портал размещает GPU узла по (cluster, hostname), алерты метят их hostname: снимок узла не может
        # описывать другой узел или кластер.
        if self.source_id != self.cluster + SOURCE_ID_SEPARATOR + self.hostname:
            raise ValueError("kubernetes-node source_id must be <cluster>.<node>, the node being the hostname")
        return self
