"""Сборка модуля telemetry: HTTP-клиенты Prometheus и Alertmanager, сервисы и оркестратор."""
from dataclasses import dataclass

import httpx

from config.settings import Settings
from telemetry.adapters.alertmanager_client import AlertmanagerClient
from telemetry.adapters.prometheus_client import PrometheusClient
from telemetry.orchestrator import TelemetryDependencies, TelemetryOrchestrator
from telemetry.services.alert_digest import AlertDigest
from telemetry.services.metric_reader import MetricReader
from telemetry.services.workload_classifier import WorkloadClassifier
from telemetry.services.workload_reader import WorkloadReader

PROMETHEUS_TIMEOUT_SECONDS = 10.0
ALERTMANAGER_TIMEOUT_SECONDS = 5.0
PARALLEL_QUERIES = 8


@dataclass(frozen=True)
class TelemetryConfig:
    """Узкая конфигурация модуля: адреса внешних систем."""

    prometheus_url: str
    alertmanager_url: str


def telemetry_config(settings: Settings) -> TelemetryConfig:
    """Выделяет из общих настроек то, что нужно модулю telemetry."""
    config = TelemetryConfig(prometheus_url=settings.prometheus_url, alertmanager_url=settings.alertmanager_url)
    return config


def build_telemetry_orchestrator(settings: Settings,
                                 http_transport: httpx.BaseTransport | None = None) -> TelemetryOrchestrator:
    """Создает адаптеры, сервисы и оркестратор; транспорт подменяется в тестах."""
    config = telemetry_config(settings)
    prometheus_http = httpx.Client(timeout=PROMETHEUS_TIMEOUT_SECONDS, transport=http_transport)
    alertmanager_http = httpx.Client(timeout=ALERTMANAGER_TIMEOUT_SECONDS, transport=http_transport)
    prometheus = PrometheusClient(config.prometheus_url, prometheus_http)
    alertmanager = AlertmanagerClient(config.alertmanager_url, alertmanager_http)
    classifier = WorkloadClassifier()
    metric_reader = MetricReader(prometheus, PARALLEL_QUERIES)
    workload_reader = WorkloadReader(prometheus, classifier, PARALLEL_QUERIES)
    alert_digest = AlertDigest(alertmanager)
    dependencies = TelemetryDependencies(metric_reader=metric_reader, workload_reader=workload_reader,
                                         alert_digest=alert_digest)
    orchestrator = TelemetryOrchestrator(dependencies)
    return orchestrator
