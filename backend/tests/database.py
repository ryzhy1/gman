import os

from sqlalchemy import create_engine, text


def database_url(tmp_path):
    """SQLite per test by default; GMAN_TEST_DATABASE_URL runs the suite against a disposable PostgreSQL DB."""
    url = os.getenv("GMAN_TEST_DATABASE_URL")
    if not url:
        return "sqlite:///" + str(tmp_path / "test.db")
    engine = create_engine(url)
    with engine.begin() as connection:
        # Start empty so that every test also exercises the migrations from scratch.
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return url
