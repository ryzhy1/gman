"""Сущности модуля exposition: прочитанное состояние inventory, TLS-файлы и выборки метрик."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class NodeReading:
    """Узел Kubernetes из последнего снимка кластера."""

    name: str
    ready: bool
    pressure: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GpuReading:
    """Показания GPU из последнего снимка; None — показание не поддерживается."""

    id: str
    vendor: str
    model: str = ""
    temperature_c: float | None = None
    ecc_uncorrected_errors: int | None = None
    # None — агент не сообщил причины троттлинга (поле отсутствует), [] — причин нет.
    throttle_reasons: list[str] | None = None


@dataclass(frozen=True)
class SourceReading:
    """Последний принятый снимок источника."""

    source_id: str
    source_type: str
    hostname: str
    received_at: datetime
    error_count: int
    nodes: list[NodeReading] = field(default_factory=list)
    gpus: list[GpuReading] = field(default_factory=list)


@dataclass(frozen=True)
class InstanceReading:
    """Наблюдаемый сейчас экземпляр."""

    source_id: str
    instance_id: str
    name: str
    runtime: str
    node: str
    namespace: str
    service_id: str
    environment: str
    restarts: int
    reason: str
    last_termination_reason: str
    last_oom_at: datetime | None


@dataclass(frozen=True)
class NodeEventTime:
    """Последнее время Event узла с данной причиной."""

    source_id: str
    node: str
    reason: str
    last_at: datetime


@dataclass(frozen=True)
class ReleaseReading:
    """Последний релиз сервиса в окружении."""

    service_id: str
    environment: str
    status: str
    started_at: datetime


@dataclass(frozen=True)
class TlsContents:
    """Сертификаты (субъект, notAfter) и CRL (издатель, nextUpdate) файла; время — Unix-секунды."""

    certificates: list[tuple[str, float]]
    crls: list[tuple[str, float]]


@dataclass(frozen=True)
class MetricSample:
    """Одна выборка текстового формата Prometheus."""

    name: str
    labels: dict[str, str]
    value: float
