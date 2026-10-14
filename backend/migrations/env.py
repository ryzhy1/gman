"""Окружение Alembic: при старте API — его соединение под блокировкой, из CLI — DATABASE_URL."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, MetaData

config = context.config


def run(connection: Connection, target_metadata: MetaData | None = None) -> None:
    """Применяет миграции в переданном соединении."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_from_cli() -> None:
    """Запуск из CLI alembic (каталог backend/): адрес БД из настроек API."""
    if config.config_file_name:
        fileConfig(config.config_file_name)
    from config.settings import Settings
    from database.engine import acquire_migration_lock, create_database_engine
    from database.tables import Base
    url = Settings().database_url
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_database_engine(url)
    with engine.begin() as connection:
        acquire_migration_lock(connection)
        run(connection, Base.metadata)
    engine.dispose()


if "connection" in config.attributes:
    # Вызов из API при старте (database.engine.apply_migrations) в его заблокированной транзакции;
    # fileConfig пропускается, чтобы не трогать настройку логирования сервера.
    run(config.attributes["connection"])
else:
    run_from_cli()
