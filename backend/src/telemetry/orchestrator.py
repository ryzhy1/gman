"""Оркестратор модуля telemetry: метрики ресурсов, состояние workloads и активные алерты."""
from dataclasses import dataclass
from typing import Any

from telemetry.entity import MetricsDisabledError, StoreUnavailableError
from telemetry.mappers import (ALERTMANAGER_DISABLED, ALERTMANAGER_UNAVAILABLE, METRICS_UNAVAILABLE,
                               PROMETHEUS_DISABLED, alerts_response, current_metrics_response,
                               range_query_from_params, range_series_to_response, workload_matchers_from_params,
                               workloads_response)
from telemetry.services.alert_digest import AlertDigest
from telemetry.services.metric_reader import MetricReader
from telemetry.services.workload_reader import WorkloadReader


@dataclass(frozen=True)
class TelemetryDependencies:
    """Готовые зависимости оркестратора telemetry."""

    metric_reader: MetricReader
    workload_reader: WorkloadReader
    alert_digest: AlertDigest


class TelemetryOrchestrator:
    """Единственная точка входа модуля telemetry. Отсутствие данных сообщается, а не превращается в нули."""

    def __init__(self, dependencies: TelemetryDependencies) -> None:
        """Запоминает сервисы модуля."""
        self._metrics = dependencies.metric_reader
        self._workloads = dependencies.workload_reader
        self._alerts = dependencies.alert_digest

    def current_metrics(self) -> dict[str, Any]:
        """Последние значения метрик всех объектов."""
        # Без адреса Prometheus метрики выключены.
        if not self._metrics.enabled:
            response = current_metrics_response(None, PROMETHEUS_DISABLED, enabled=False)
            return response
        # Читаем последние значения всех шаблонов; недоступное хранилище — причина в ответе.
        try:
            values = self._metrics.current()
        except StoreUnavailableError as error:
            response = current_metrics_response(None, METRICS_UNAVAILABLE + str(error), enabled=True)
            return response
        # Собираем ответ.
        response = current_metrics_response(values, None, enabled=True)
        return response

    def metric_range(self, params: dict[str, Any]) -> dict[str, Any]:
        """Ряд одной метрики одного объекта за период."""
        # Без адреса Prometheus ряд построить нельзя.
        if not self._metrics.enabled:
            raise MetricsDisabledError(PROMETHEUS_DISABLED)
        # Параметры HTTP -> запрос ряда.
        query = range_query_from_params(params)
        # Читаем ряд по фиксированному шаблону.
        series = self._metrics.series(query)
        # Собираем ответ.
        response = range_series_to_response(series)
        return response

    def workloads(self, params: dict[str, str | None]) -> dict[str, Any]:
        """Workloads Kubernetes сервиса (и окружения) или кластера."""
        # Параметры HTTP -> матчеры; без сервиса и кластера запрос некорректен.
        matchers = workload_matchers_from_params(params)
        # Без адреса Prometheus workloads выключены.
        if not self._workloads.enabled:
            response = workloads_response([], PROMETHEUS_DISABLED, enabled=False)
            return response
        # Читаем и классифицируем workloads; недоступное хранилище — причина в ответе.
        try:
            workloads = self._workloads.workloads(matchers)
        except StoreUnavailableError as error:
            response = workloads_response([], METRICS_UNAVAILABLE + str(error), enabled=True)
            return response
        # Собираем ответ.
        response = workloads_response(workloads, None, enabled=True)
        return response

    def active_alerts(self) -> dict[str, Any]:
        """Сработавшие алерты Alertmanager без заглушенных и подавленных."""
        # Без адреса Alertmanager алерты выключены.
        if not self._alerts.enabled:
            response = alerts_response([], ALERTMANAGER_DISABLED, enabled=False)
            return response
        # Читаем и упорядочиваем алерты; недоступный Alertmanager — причина в ответе.
        try:
            alerts = self._alerts.active()
        except StoreUnavailableError as error:
            response = alerts_response([], ALERTMANAGER_UNAVAILABLE + str(error), enabled=True)
            return response
        # Собираем ответ.
        response = alerts_response(alerts, None, enabled=True)
        return response
