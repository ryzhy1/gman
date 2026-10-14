"""Оркестратор модуля deployment_report: явное утверждение о деплое из deployment job Bamboo."""
from collections.abc import Mapping
from dataclasses import dataclass

from deployment_report.entity import JsonObject
from deployment_report.mappers import context_from_environment
from deployment_report.services.deployment_event import DeploymentEventBuilder
from deployment_report.services.deployment_publisher import DeploymentPublisher


@dataclass(frozen=True)
class DeploymentReportDependencies:
    """Готовые зависимости оркестратора deployment_report."""

    builder: DeploymentEventBuilder
    publisher: DeploymentPublisher


class DeploymentReportOrchestrator:
    """Единственная точка входа модуля deployment_report."""

    def __init__(self, dependencies: DeploymentReportDependencies) -> None:
        """Запоминает построение и публикацию события."""
        self._builder = dependencies.builder
        self._publisher = dependencies.publisher

    def event(self, environ: Mapping[str, str]) -> JsonObject:
        """Событие деплоя по окружению шага Bamboo."""
        # Окружение -> сведения о деплое.
        context = context_from_environment(environ)
        # Сведения -> событие со стабильным ID и инициатором.
        event = self._builder.build(context)
        return event

    def send(self, environ: Mapping[str, str]) -> str:
        """Строит и отправляет событие; возвращает строку статуса ответа API."""
        # Строим событие.
        event = self.event(environ)
        # Отправляем в GMan API.
        status = self._publisher.publish(event)
        return status
