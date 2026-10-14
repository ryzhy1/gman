"""Входящий порт GMan API (FastAPI): собирает контейнеры модулей, создает оркестраторы и подключает маршруты.

Запуск: uvicorn main:create_app --factory --app-dir src. API без аутентификации: доступ к нему
ограничивается на уровне сети.
"""
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import Engine

from api.dependencies import Orchestrators
from api.errors import register_error_handlers
from api.routes import configs, inventory, releases, system, telemetry
from config.logging import configure_logging
from config.settings import Settings
from config_history.container import build_config_history_orchestrator
from database.engine import create_database_engine
from database.unit_of_work import UnitOfWork
from exposition.container import build_exposition_orchestrator
from inventory.container import build_inventory_orchestrator
from lifespan import build_lifespan
from releases.container import build_releases_orchestrator
from telemetry.container import build_telemetry_orchestrator

TITLE = "GMan Inventory"
VERSION = "0.2.0"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def create_app(settings: Settings | None = None, http_transport: httpx.BaseTransport | None = None) -> FastAPI:
    """Приложение API; транспорт HTTP к Prometheus и Alertmanager подменяется в тестах."""
    resolved = settings or Settings()
    configure_logging(resolved.log_level)
    engine = create_database_engine(resolved.database_url)
    orchestrators = build_orchestrators(resolved, engine, http_transport)
    lifespan = build_lifespan(engine, MIGRATIONS_DIR)
    app = FastAPI(title=TITLE, version=VERSION, lifespan=lifespan)
    app.state.orchestrators = orchestrators
    register_error_handlers(app)
    for module in (system, inventory, configs, releases, telemetry):
        app.include_router(module.router)
    return app


def build_orchestrators(settings: Settings, engine: Engine,
                        http_transport: httpx.BaseTransport | None) -> Orchestrators:
    """Контейнеры модулей поверх одной границы транзакций; inventory учитывает релизы через оркестратор releases."""
    unit_of_work = UnitOfWork(engine)
    releases_orchestrator = build_releases_orchestrator(settings, unit_of_work)
    inventory_orchestrator = build_inventory_orchestrator(settings, unit_of_work, releases_orchestrator)
    config_history_orchestrator = build_config_history_orchestrator(unit_of_work)
    telemetry_orchestrator = build_telemetry_orchestrator(settings, http_transport)
    exposition_orchestrator = build_exposition_orchestrator(settings, unit_of_work)
    orchestrators = Orchestrators(inventory=inventory_orchestrator, config_history=config_history_orchestrator,
                                  releases=releases_orchestrator, telemetry=telemetry_orchestrator,
                                  exposition=exposition_orchestrator)
    return orchestrators
