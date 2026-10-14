"""Общие фикстуры тестов GMan API: своя БД на тест и клиенты приложения с настройками и подменой HTTP."""
import os
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from config.settings import Settings
from main import create_app

# Переменные окружения, которые читает Settings: тесты не должны зависеть от окружения разработчика.
SETTINGS_ENV = ("DATABASE_URL", "PROMETHEUS_URL", "ALERTMANAGER_URL", "GMAN_GRAFANA_URL", "GMAN_METRICS_INTERVAL",
                "GMAN_HISTORY_GAP_SECONDS", "GMAN_RETENTION_DAYS", "GMAN_RELEASE_TIMEOUT_MINUTES",
                "GMAN_RELEASE_LEAD_MINUTES", "GMAN_RELEASE_CRASH_RESTARTS", "GMAN_SPLUNK_URL", "GMAN_SPLUNK_SEARCH",
                "GMAN_SPLUNK_INDEXES", "GMAN_TLS_WATCH_FILES", "GMAN_LOG_LEVEL")
TEST_DATABASE_ENV = "GMAN_TEST_DATABASE_URL"

ClientFactory = Callable[..., TestClient]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убирает настройки API из окружения процесса тестов."""
    for name in SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def database_url(tmp_path) -> str:
    """SQLite на тест; GMAN_TEST_DATABASE_URL гоняет тесты на одноразовой БД PostgreSQL."""
    url = os.getenv(TEST_DATABASE_ENV)
    if not url:
        return "sqlite:///" + str(tmp_path / "test.db")
    engine = create_engine(url)
    with engine.begin() as connection:
        # Пустая схема: каждый тест заодно проверяет миграции с нуля.
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return url


@pytest.fixture
def make_client(database_url: str) -> Iterator[ClientFactory]:
    """Фабрика клиентов приложения над БД теста: настройки и HTTP-транспорт задаются на клиента."""
    clients = []

    def build(transport: httpx.BaseTransport | None = None, **overrides: object) -> TestClient:
        """Клиент приложения с переопределенными полями Settings."""
        settings = Settings(database_url=database_url, **overrides)
        client = TestClient(create_app(settings, transport), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in reversed(clients):
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    """Клиент приложения с настройками по умолчанию."""
    return make_client()


@pytest.fixture
def engine(client: TestClient, database_url: str) -> Iterator[Engine]:
    """Engine для прямых проверок БД (схема уже создана клиентом)."""
    database = create_engine(database_url)
    yield database
    database.dispose()


def orchestrators(client: TestClient):
    """Оркестраторы приложения клиента (для сценариев вне HTTP: обслуживание, тайм-ауты)."""
    return client.app.state.orchestrators


@pytest.fixture
def configured(make_client: ClientFactory) -> Callable[..., TestClient]:
    """Фабрика клиентов с настроенным Prometheus, который отвечает функцией handler(request)."""
    def connect(handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
        """Клиент, чьи запросы к Prometheus обслуживает handler."""
        return make_client(transport=httpx.MockTransport(handler), prometheus_url="http://prom:9090/")
    return connect
