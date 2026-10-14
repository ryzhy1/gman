"""Сущности модуля telemetry: выборки Prometheus, состояние workloads, активные алерты."""
from dataclasses import dataclass, field


class TelemetryError(Exception):
    """Базовая ошибка модуля telemetry."""


class StoreUnavailableError(TelemetryError):
    """Хранилище метрик или Alertmanager недоступны либо ответили ошибкой."""


class UnknownQueryError(TelemetryError):
    """Неизвестная метрика, диапазон, объект или недопустимый идентификатор."""


class MissingWorkloadFilterError(TelemetryError):
    """Для workloads не задан ни сервис, ни кластер."""


class MetricsDisabledError(TelemetryError):
    """Адрес Prometheus не настроен."""


@dataclass(frozen=True)
class Sample:
    """Один ряд мгновенного вектора Prometheus: метки и значение (None для NaN/Inf)."""

    labels: dict[str, str]
    value: float | None


@dataclass(frozen=True)
class SeriesPoint:
    """Точка ряда: Unix-время и значение."""

    timestamp: float
    value: float | None


@dataclass(frozen=True)
class RangeQuery:
    """Запрос ряда метрики одного объекта за период."""

    scope: str
    metric: str
    matchers: dict[str, str]
    range_name: str


@dataclass(frozen=True)
class RangeSeries:
    """Ряд метрики объекта за период."""

    scope: str
    metric: str
    range_name: str
    step: int
    points: list[SeriesPoint]


@dataclass(frozen=True)
class RawAlert:
    """Алерт из Alertmanager до фильтрации."""

    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: str | None


@dataclass(frozen=True)
class ActiveAlert:
    """Активный алерт для портала."""

    name: str
    starts_at: str | None
    labels: dict[str, str]
    summary: str
    description: str
    runbook_url: str


@dataclass(frozen=True)
class WorkloadAlert:
    """Сработавший алерт workload-а."""

    name: str
    severity: str
    reason: str


@dataclass
class Workload:
    """Workload Kubernetes (Deployment, StatefulSet, DaemonSet, Job) по метрикам kube-state-metrics."""

    cluster: str
    namespace: str
    kind: str
    name: str
    service_id: str
    environment: str
    desired: int | None = None
    ready: int | None = None
    available: int | None = None
    updated: int | None = None
    unavailable: int | None = None
    generation: int | None = None
    observed_generation: int | None = None
    pending_pods: int | None = None
    paused: bool = False
    progressing: bool | None = None
    progressing_reason: str = ""
    current_revision: str = ""
    update_revision: str = ""
    failed: bool | None = None
    active: int | None = None
    alerts: list[WorkloadAlert] = field(default_factory=list)
    status: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass
class WorkloadReadings:
    """Сырые показания workload-а, собранные из ответов шаблонов."""

    counts: dict[str, int | None] = field(default_factory=dict)
    labeled: dict[str, list[dict[str, str]]] = field(default_factory=dict)
