"""Отправка снимка в GMan API (POST /api/inventory) с классификацией отказов."""
import json
import logging
from collections.abc import Callable
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from delivery.entity import BlockedError, JsonObject, RejectedError, RetryableError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
RESPONSE_PREVIEW_BYTES = 500
ERROR_PREVIEW_BYTES = 1000
RETRYABLE_CODES = (408, 425, 429)
SERVER_ERROR = 500
BLOCKED_CODES = (401, 403)
JSON_HEADERS = {"Content-Type": "application/json"}


class InventoryApi:
    """HTTP-клиент приема снимков; API без аутентификации, доступ к нему ограничивается сетью."""

    def __init__(self, endpoint: str, opener: Callable[..., Any] = urlopen) -> None:
        """Запоминает адрес приема снимков и функцию открытия запросов."""
        self._endpoint = endpoint
        self._opener = opener

    def post(self, snapshot: JsonObject) -> None:
        """Отправляет снимок; 200 с accepted=false — повтор или более старый снимок, повтор идемпотентен."""
        request = Request(self._endpoint, data=json.dumps(snapshot).encode(), headers=JSON_HEADERS)
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read(RESPONSE_PREVIEW_BYTES).decode(errors="replace")
                logger.info("snapshot %s delivered: HTTP %s %s", snapshot.get("source_id"), response.status, body)
        except HTTPError as error:
            raise _classified(error) from None
        except (URLError, OSError, HTTPException) as error:
            raise RetryableError(type(error).__name__) from None


def _classified(error: HTTPError) -> Exception:
    """Отказ по коду HTTP: временный, блокировка агента или окончательный отказ снимку."""
    # Тело ошибки API не содержит inventory и учетных данных.
    detail = f"HTTP {error.code} {error.read(ERROR_PREVIEW_BYTES).decode(errors='replace')}"
    if error.code in RETRYABLE_CODES or error.code >= SERVER_ERROR:
        classified: Exception = RetryableError(detail)
    elif error.code in BLOCKED_CODES:
        classified = BlockedError(detail)
    else:
        classified = RejectedError(detail)
    return classified
