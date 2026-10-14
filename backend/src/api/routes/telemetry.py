"""Маршруты метрик ресурсов, состояния workloads и активных алертов (только чтение)."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api import dependencies
from telemetry.entity import MetricsDisabledError, MissingWorkloadFilterError, StoreUnavailableError, UnknownQueryError
from telemetry.orchestrator import TelemetryOrchestrator

router = APIRouter()

LABEL_LENGTH = 300
METRIC_LENGTH = 100
RANGE_LENGTH = 10
METRICS_UNAVAILABLE = "Metrics store unavailable: "
Scope = Literal["host", "container", "gpu", "k8s_container", "k8s_pod", "k8s_gpu", "llm"]


def _label(default: str | None = None) -> Any:
    """Необязательный параметр-идентификатор объекта."""
    parameter = Query(default, max_length=LABEL_LENGTH)
    return parameter


@router.get("/api/metrics/current")
def metrics_current(orchestrator: TelemetryOrchestrator = Depends(dependencies.telemetry)) -> dict[str, Any]:
    """Последнее потребление ресурсов; отсутствие данных сообщается, а не превращается в нули."""
    response = orchestrator.current_metrics()
    return response


@router.get("/api/metrics/range")
def metrics_range(scope: Scope, metric: str = Query(max_length=METRIC_LENGTH),
                  range_: str = Query("1h", alias="range", max_length=RANGE_LENGTH),
                  host_id: str | None = _label(), container_id: str | None = _label(), uuid: str | None = _label(),
                  pod_uid: str | None = _label(), container_name: str | None = _label(),
                  namespace: str | None = _label(), pod_name: str | None = _label(),
                  service_id: str | None = _label(), model: str | None = _label(),
                  orchestrator: TelemetryOrchestrator = Depends(dependencies.telemetry)) -> dict[str, Any]:
    """Параметры объекта по области: host: host_id; container: host_id, container_id; gpu: host_id, uuid;
    k8s_container: pod_uid, container_name; k8s_pod: pod_uid; k8s_gpu: namespace, pod_name, container_name;
    llm: service_id, model. Любая другая комбинация — 422."""
    params = {"scope": scope, "metric": metric, "range": range_, "host_id": host_id, "container_id": container_id,
              "uuid": uuid, "pod_uid": pod_uid, "container_name": container_name, "namespace": namespace,
              "pod_name": pod_name, "service_id": service_id, "model": model}
    try:
        response = orchestrator.metric_range(params)
    except MetricsDisabledError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    except UnknownQueryError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown metric, range or object") from error
    except StoreUnavailableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, METRICS_UNAVAILABLE + str(error)) from error
    return response


@router.get("/api/workloads")
def workload_state(service_id: str | None = _label(), environment: str | None = _label(),
                   cluster: str | None = _label(),
                   orchestrator: TelemetryOrchestrator = Depends(dependencies.telemetry)) -> dict[str, Any]:
    """Workloads Kubernetes сервиса (можно одного окружения) или кластера из kube-state-metrics."""
    params = {"service_id": service_id, "environment": environment, "cluster": cluster}
    try:
        response = orchestrator.workloads(params)
    except MissingWorkloadFilterError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "service_id or cluster is required") from error
    except UnknownQueryError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid service, environment or cluster") from error
    return response


@router.get("/api/alerts")
def active_alerts(orchestrator: TelemetryOrchestrator = Depends(dependencies.telemetry)) -> dict[str, Any]:
    """Сработавшие алерты Alertmanager без заглушенных и подавленных."""
    response = orchestrator.active_alerts()
    return response
