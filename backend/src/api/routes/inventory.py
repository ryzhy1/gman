"""Маршруты inventory: прием снимков, каталог сервисов, обзор, аудит и ссылки на логи.

API без аутентификации: доступ к нему ограничивается на уровне сети.
"""
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api import dependencies
from api.schemas.catalog import Service
from api.schemas.inventory import Snapshot
from inventory.entity import (DuplicateInstanceError, InvalidCursorError, ServiceIdMismatchError,
                              SplunkTemplateError, UnknownServiceError)
from inventory.orchestrator import InventoryOrchestrator

router = APIRouter()

ID_LENGTH = 100
ENVIRONMENT_LENGTH = 200
CURSOR_LENGTH = 200
DEFAULT_PAGE = 200
MAX_PAGE = 1000
JSON_MODE = "json"


@router.post("/api/inventory")
def ingest(value: Snapshot,
           orchestrator: InventoryOrchestrator = Depends(dependencies.inventory)) -> dict[str, Any]:
    """Снимок Docker-хоста, кластера или GPU-узла от агента."""
    payload = value.model_dump(mode=JSON_MODE)
    try:
        response = orchestrator.ingest(payload)
    except DuplicateInstanceError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Duplicate instance IDs") from error
    return response


@router.put("/api/services/{service_id}")
def put_service(service_id: str, value: Service,
                orchestrator: InventoryOrchestrator = Depends(dependencies.inventory)) -> dict[str, Any]:
    """Создает или обновляет сервис каталога; необязательные разделы без значения не сохраняются."""
    payload = value.model_dump(exclude_none=True)
    try:
        response = orchestrator.register_service(service_id, payload)
    except ServiceIdMismatchError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "ID mismatch") from error
    except SplunkTemplateError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    return response


@router.get("/api/overview")
def overview(orchestrator: InventoryOrchestrator = Depends(dependencies.inventory)) -> dict[str, Any]:
    """Каталог, источники с экземплярами и последние события для портала."""
    response = orchestrator.overview()
    return response


@router.get("/api/events")
def list_events(limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE), before: str = Query("", max_length=CURSOR_LENGTH),
                service_id: str | None = Query(None, max_length=ID_LENGTH),
                orchestrator: InventoryOrchestrator = Depends(dependencies.inventory)) -> dict[str, Any]:
    """Аудит новыми первыми; `next` из ответа передается как `before` для следующей страницы."""
    try:
        response = orchestrator.history(limit, service_id, before)
    except InvalidCursorError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from error
    return response


@router.get("/api/logs/splunk")
def splunk_links(service_id: str = Query(max_length=ID_LENGTH),
                 environment: str = Query("", max_length=ENVIRONMENT_LENGTH),
                 range_: Literal["1h", "24h", "7d"] = Query("1h", alias="range"),
                 orchestrator: InventoryOrchestrator = Depends(dependencies.inventory)) -> dict[str, Any]:
    """Ссылки на поиск Splunk для сервиса и его экземпляров; значения экранируются в SPL на сервере."""
    try:
        response = orchestrator.splunk_links(service_id, environment, range_)
    except UnknownServiceError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown service") from error
    return response
