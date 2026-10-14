"""Маппинг модуля telemetry: параметры запросов -> DTO, DTO -> ответы API."""
from dataclasses import asdict
from typing import Any

from telemetry.entity import ActiveAlert, MissingWorkloadFilterError, RangeQuery, RangeSeries, Workload

K8S_GPU_SCOPE = "k8s_gpu"
# DCGM метит pod-ы по имени, kubelet_stats — по UID: один параметр container_name ложится на разные метки.
DCGM_CONTAINER_LABEL = "container"
KUBELET_CONTAINER_LABEL = "k8s_container_name"
PROMETHEUS_DISABLED = "PROMETHEUS_URL is not configured"
ALERTMANAGER_DISABLED = "ALERTMANAGER_URL is not configured"
METRICS_UNAVAILABLE = "Metrics store unavailable: "
ALERTMANAGER_UNAVAILABLE = "Alertmanager unavailable: "


def range_query_from_params(params: dict[str, Any]) -> RangeQuery:
    """Запрос ряда из параметров HTTP: объект задается метками, зависящими от области."""
    scope = params["scope"]
    container_label = DCGM_CONTAINER_LABEL if scope == K8S_GPU_SCOPE else KUBELET_CONTAINER_LABEL
    candidates = {"host_id": params.get("host_id"), "container_id": params.get("container_id"),
                  "UUID": params.get("uuid"), "k8s_pod_uid": params.get("pod_uid"),
                  container_label: params.get("container_name"), "namespace": params.get("namespace"),
                  "pod": params.get("pod_name"), "gman_service": params.get("service_id"),
                  "model_name": params.get("model")}
    matchers = {label: value for label, value in candidates.items() if value is not None}
    query = RangeQuery(scope=scope, metric=params["metric"], matchers=matchers, range_name=params["range"])
    return query


def workload_matchers_from_params(params: dict[str, str | None]) -> dict[str, str]:
    """Матчеры workloads: сервис (и окружение) или кластер; пустые значения не участвуют."""
    if not params.get("service_id") and not params.get("cluster"):
        raise MissingWorkloadFilterError("service_id or cluster is required")
    candidates = {"service_id": params.get("service_id"), "environment": params.get("environment"),
                  "source_id": params.get("cluster")}
    matchers = {label: value for label, value in candidates.items() if value}
    return matchers


def range_series_to_response(series: RangeSeries) -> dict[str, Any]:
    """Ряд метрики в формате ответа API."""
    points = [[point.timestamp, point.value] for point in series.points]
    response = {"scope": series.scope, "metric": series.metric, "range": series.range_name, "step": series.step,
                "points": points}
    return response


def current_metrics_response(values: dict[str, Any] | None, error: str | None, enabled: bool) -> dict[str, Any]:
    """Ответ с последними значениями метрик или с причиной их отсутствия."""
    response: dict[str, Any] = {"enabled": enabled, "error": error}
    if values is not None:
        response.update(values)
    return response


def workloads_response(workloads: list[Workload], error: str | None, enabled: bool) -> dict[str, Any]:
    """Ответ со списком workloads или с причиной его отсутствия."""
    items = [asdict(workload) for workload in workloads]
    response = {"enabled": enabled, "error": error, "workloads": items}
    return response


def alerts_response(alerts: list[ActiveAlert], error: str | None, enabled: bool) -> dict[str, Any]:
    """Ответ с активными алертами или с причиной их отсутствия."""
    items = [asdict(alert) for alert in alerts]
    response = {"enabled": enabled, "error": error, "alerts": items}
    return response
