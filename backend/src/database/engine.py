"""Создание engine SQLAlchemy и применение миграций при старте."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text

from database.column_types import POSTGRESQL

POSTGRESQL_URL_PREFIX = "postgresql"
MIGRATION_LOCK = text("SELECT pg_advisory_xact_lock(hashtext('gman:migrations'))")
MIGRATION_TARGET = "head"


def create_database_engine(url: str) -> Engine:
    """Создает engine; для PostgreSQL проверяет соединения пула перед использованием."""
    engine = create_engine(url, pool_pre_ping=url.startswith(POSTGRESQL_URL_PREFIX))
    return engine


def acquire_migration_lock(connection: Connection) -> None:
    """Несколько реплик API могут стартовать одновременно: DDL выполняет только одна."""
    if connection.dialect.name == POSTGRESQL:
        connection.execute(MIGRATION_LOCK)


def apply_migrations(engine: Engine, script_location: Path) -> None:
    """Применяет миграции Alembic до head под блокировкой миграций."""
    config = Config()
    config.set_main_option("script_location", str(script_location))
    with engine.begin() as connection:
        acquire_migration_lock(connection)
        config.attributes["connection"] = connection
        command.upgrade(config, MIGRATION_TARGET)
