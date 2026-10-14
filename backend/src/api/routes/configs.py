"""Маршруты истории конфигураций: версии сервиса и экземпляра, версия и сравнение двух версий."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from api import dependencies
from config_history.entity import InvalidCursorError, UnknownInstanceError, UnknownServiceError, UnknownVersionError
from config_history.orchestrator import ConfigHistoryOrchestrator

router = APIRouter()

# BIGINT в PostgreSQL.
MAX_ID = 2 ** 63 - 1
DEFAULT_PAGE = 100
MAX_PAGE = 1000
CURSOR_LENGTH = 200


@router.get("/api/services/{service_id}/configs")
def service_configs(service_id: str, limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
                    before: str = Query("", max_length=CURSOR_LENGTH),
                    orchestrator: ConfigHistoryOrchestrator = Depends(dependencies.config_history)) -> dict[str, Any]:
    """Версии конфигураций всех экземпляров сервиса, новые первыми, без тел конфигураций."""
    try:
        response = orchestrator.service_versions(service_id, limit, before)
    except UnknownServiceError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found") from error
    except InvalidCursorError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from error
    return response


@router.get("/api/instances/{source_id}/{instance_id}/configs")
def instance_configs(source_id: str, instance_id: str, limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
                     before: str = Query("", max_length=CURSOR_LENGTH),
                     orchestrator: ConfigHistoryOrchestrator = Depends(dependencies.config_history)) -> dict[str, Any]:
    """Версии конфигураций одного экземпляра."""
    try:
        response = orchestrator.instance_versions(source_id, instance_id, limit, before)
    except UnknownInstanceError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instance not found") from error
    except InvalidCursorError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from error
    return response


# Объявлен раньше /api/configs/{version_id}, чтобы "diff" не принимался за ID.
@router.get("/api/configs/diff")
def config_diff(from_: int = Query(alias="from", ge=1, le=MAX_ID), to: int = Query(ge=1, le=MAX_ID),
                orchestrator: ConfigHistoryOrchestrator = Depends(dependencies.config_history)) -> dict[str, Any]:
    """Изменения от версии `from` к версии `to`; сравнивать можно любые две версии."""
    try:
        response = orchestrator.compare(from_, to)
    except UnknownVersionError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    return response


@router.get("/api/configs/{version_id}")
def config_version(version_id: int = Path(ge=1, le=MAX_ID),
                   orchestrator: ConfigHistoryOrchestrator = Depends(dependencies.config_history)) -> dict[str, Any]:
    """Версия конфигурации с телом."""
    try:
        response = orchestrator.version(version_id)
    except UnknownVersionError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    return response
