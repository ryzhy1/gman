"""Отправка события деплоя в GMan API (POST /api/deployments)."""
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from deployment_report.entity import DeploymentApiError, JsonObject

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
RESPONSE_PREVIEW_BYTES = 500
ERROR_PREVIEW_BYTES = 1000
JSON_HEADERS = {"Content-Type": "application/json"}


class DeploymentsApi:
    """HTTP-клиент приема событий деплоя."""

    def __init__(self, endpoint: str, opener: Callable[..., Any] = urlopen) -> None:
        """Запоминает адрес приема и функцию открытия запросов."""
        self._endpoint = endpoint
        self._opener = opener

    def post(self, event: JsonObject) -> str:
        """Отправляет событие; возвращает строку статуса ответа, отказ API — DeploymentApiError."""
        request = Request(self._endpoint, data=json.dumps(event).encode(), headers=JSON_HEADERS)
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status = f"HTTP {response.status} {response.read(RESPONSE_PREVIEW_BYTES).decode(errors='replace')}"
        except HTTPError as error:
            detail = f"HTTP {error.code} {error.read(ERROR_PREVIEW_BYTES).decode(errors='replace')}"
            raise DeploymentApiError(detail) from None
        return status
