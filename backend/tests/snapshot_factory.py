"""Снимки inventory для тестов API."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from httpx import Response


def snapshot(offset: float = 0) -> dict:
    """Снимок Docker-хоста node-1 с одним контейнером; offset — сдвиг времени наблюдения в секундах."""
    return {"source_id": "node-1", "source_type": "docker-host", "hostname": "host",
            "observed_at": (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(),
            "instances": [{"id": "container-1", "name": "api", "runtime": "docker", "status": "running",
                           "service_id": "api", "started_at": "2026-10-10T10:00:00.123456789Z",
                           "config": {"image": "api:v1"}}]}


def post(client: TestClient, value: dict) -> Response:
    """Отправляет снимок в API."""
    return client.post("/api/inventory", json=value)
