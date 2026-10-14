"""Технические маршруты: проверка живости и метрики inventory для Prometheus."""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from api import dependencies
from exposition.orchestrator import ExpositionOrchestrator

router = APIRouter()

PROMETHEUS_TEXT_FORMAT = "text/plain; version=0.0.4"


@router.get("/healthz")
def health() -> dict[str, str]:
    """Процесс API отвечает."""
    return {"status": "ok"}


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics(orchestrator: ExpositionOrchestrator = Depends(dependencies.exposition)) -> PlainTextResponse:
    """Метрики inventory для правил алертов. Внутренний путь: nginx его не проксирует."""
    text = orchestrator.render()
    response = PlainTextResponse(text, media_type=PROMETHEUS_TEXT_FORMAT)
    return response
