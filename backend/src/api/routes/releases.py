"""Маршруты релизов: события деплоя из Bamboo и чтение релизов."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api import dependencies
from api.schemas.deployments import Deployment
from releases.entity import STATUSES, DeploymentConflictError, InvalidCursorError, ReleaseNotFoundError
from releases.orchestrator import ReleasesOrchestrator

router = APIRouter()

ID_LENGTH = 100
CURSOR_LENGTH = 200
DEFAULT_PAGE = 50
MAX_PAGE = 500
JSON_MODE = "json"


@router.post("/api/deployments")
def deployment(value: Deployment,
               orchestrator: ReleasesOrchestrator = Depends(dependencies.releases)) -> dict[str, Any]:
    """Событие деплоя Bamboo; повтор того же события — не ошибка, тот же ID с другим содержимым — 409."""
    payload = value.model_dump(mode=JSON_MODE)
    try:
        response = orchestrator.record_deployment(payload)
    except DeploymentConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "Event ID reused with different payload") from error
    return response


@router.get("/api/releases")
def list_releases(service_id: str | None = Query(None, max_length=ID_LENGTH),
                  environment: str | None = Query(None, max_length=ID_LENGTH),
                  status_: Literal[STATUSES] | None = Query(None, alias="status"),
                  limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
                  before: str = Query("", max_length=CURSOR_LENGTH),
                  orchestrator: ReleasesOrchestrator = Depends(dependencies.releases)) -> dict[str, Any]:
    """Релизы новыми первыми по началу; `next` передается как `before` для следующей страницы."""
    params = {"service_id": service_id, "environment": environment, "status": status_, "limit": limit,
              "before": before}
    try:
        response = orchestrator.releases(params)
    except InvalidCursorError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from error
    return response


@router.get("/api/releases/{release_id}")
def release_detail(release_id: int,
                   orchestrator: ReleasesOrchestrator = Depends(dependencies.releases)) -> dict[str, Any]:
    """Релиз со связанными событиями и текущими экземплярами раскатки."""
    try:
        response = orchestrator.release(release_id)
    except ReleaseNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Release not found") from error
    return response
