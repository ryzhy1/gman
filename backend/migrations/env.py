import os
from logging.config import fileConfig

from alembic import context

config = context.config


def run(connection, target_metadata=None):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if "connection" in config.attributes:
    # Called from the API on startup (app.db.migrate) inside its locked transaction;
    # fileConfig is skipped there so the server's logging setup is left alone.
    run(config.attributes["connection"])
else:
    if config.config_file_name:
        fileConfig(config.config_file_name)
    from app.db import Base, connect, migration_lock
    url = os.getenv("DATABASE_URL", "sqlite:///./gman.db")
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = connect(url)
        with engine.begin() as connection:
            migration_lock(connection)
            run(connection, Base.metadata)
        engine.dispose()
