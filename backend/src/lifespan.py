"""Жизненный цикл процесса API: миграции схемы при старте и закрытие пула соединений при остановке."""
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import Engine

from database.engine import apply_migrations

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_lifespan(engine: Engine, migrations: Path) -> Lifespan:
    """Lifespan, применяющий миграции до приема запросов.

    Миграции здесь, а не в команде контейнера: один путь для Docker, локального запуска и тестов;
    одновременно стартующие реплики сериализуются advisory-блокировкой PostgreSQL.
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Миграции при старте, освобождение соединений при остановке."""
        apply_migrations(engine, migrations)
        yield
        engine.dispose()
    return lifespan
