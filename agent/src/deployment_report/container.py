"""Сборка модуля deployment_report: клиент API приема событий деплоя и построение события."""
from collections.abc import Callable
from typing import Any
from urllib.request import urlopen

from config.settings import AgentSettings
from deployment_report.adapters.deployments_api import DeploymentsApi
from deployment_report.orchestrator import DeploymentReportDependencies, DeploymentReportOrchestrator
from deployment_report.services.deployment_event import DeploymentEventBuilder
from deployment_report.services.deployment_publisher import DeploymentPublisher

HTTP_SCHEMES = ("https://", "http://")
DEPLOYMENTS_PATH = "/api/deployments"


def deployments_endpoint(settings: AgentSettings) -> str:
    """Адрес приема событий деплоя из GMAN_URL."""
    url = settings.gman_url.rstrip("/")
    if not url.startswith(HTTP_SCHEMES):
        raise ValueError("GMAN_URL must be an HTTP(S) URL")
    endpoint = url + DEPLOYMENTS_PATH
    return endpoint


def build_deployment_report(settings: AgentSettings,
                            opener: Callable[..., Any] = urlopen) -> DeploymentReportOrchestrator:
    """Создает адаптер, сервис и оркестратор."""
    endpoint = deployments_endpoint(settings)
    api = DeploymentsApi(endpoint, opener)
    publisher = DeploymentPublisher(api)
    builder = DeploymentEventBuilder()
    dependencies = DeploymentReportDependencies(builder=builder, publisher=publisher)
    orchestrator = DeploymentReportOrchestrator(dependencies)
    return orchestrator
