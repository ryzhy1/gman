"""Публикация события деплоя в GMan API."""
from deployment_report.adapters.deployments_api import DeploymentsApi
from deployment_report.entity import JsonObject


class DeploymentPublisher:
    """Отправляет событие деплоя."""

    def __init__(self, api: DeploymentsApi) -> None:
        """Запоминает клиент API."""
        self._api = api

    def publish(self, event: JsonObject) -> str:
        """Отправляет событие; возвращает строку статуса ответа."""
        status = self._api.post(event)
        return status
