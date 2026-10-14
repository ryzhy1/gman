"""Оркестраторы доменных модулей для маршрутов: собираются в main.py и хранятся в состоянии приложения."""
from dataclasses import dataclass

from fastapi import Request

from config_history.orchestrator import ConfigHistoryOrchestrator
from exposition.orchestrator import ExpositionOrchestrator
from inventory.orchestrator import InventoryOrchestrator
from releases.orchestrator import ReleasesOrchestrator
from telemetry.orchestrator import TelemetryOrchestrator


@dataclass(frozen=True)
class Orchestrators:
    """Публичные точки входа всех доменных модулей."""

    inventory: InventoryOrchestrator
    config_history: ConfigHistoryOrchestrator
    releases: ReleasesOrchestrator
    telemetry: TelemetryOrchestrator
    exposition: ExpositionOrchestrator


def _orchestrators(request: Request) -> Orchestrators:
    """Оркестраторы приложения, обрабатывающего запрос."""
    orchestrators = request.app.state.orchestrators
    return orchestrators


def inventory(request: Request) -> InventoryOrchestrator:
    """Оркестратор inventory."""
    orchestrator = _orchestrators(request).inventory
    return orchestrator


def config_history(request: Request) -> ConfigHistoryOrchestrator:
    """Оркестратор истории конфигураций."""
    orchestrator = _orchestrators(request).config_history
    return orchestrator


def releases(request: Request) -> ReleasesOrchestrator:
    """Оркестратор релизов."""
    orchestrator = _orchestrators(request).releases
    return orchestrator


def telemetry(request: Request) -> TelemetryOrchestrator:
    """Оркестратор метрик и алертов."""
    orchestrator = _orchestrators(request).telemetry
    return orchestrator


def exposition(request: Request) -> ExpositionOrchestrator:
    """Оркестратор /metrics."""
    orchestrator = _orchestrators(request).exposition
    return orchestrator
