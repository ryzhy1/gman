"""Адаптер API v2 Alertmanager (только чтение): активные алерты без заглушенных и подавленных."""
from typing import Any

import httpx

from telemetry.entity import RawAlert, StoreUnavailableError

ALERTS_PATH = "/api/v2/alerts"
ACTIVE_ONLY = {"active": "true", "silenced": "false", "inhibited": "false"}
HTTP_OK = 200


class AlertmanagerClient:
    """Читает активные алерты и возвращает их как сущности."""

    def __init__(self, base_url: str, http_client: httpx.Client) -> None:
        """Запоминает адрес Alertmanager и HTTP-клиент."""
        self._base_url = base_url
        self._http = http_client

    @property
    def enabled(self) -> bool:
        """Задан ли адрес Alertmanager."""
        configured = bool(self._base_url)
        return configured

    def active_alerts(self) -> list[RawAlert]:
        """Сработавшие алерты, которые не заглушены и не подавлены."""
        body = self._fetch()
        alerts = []
        for item in body:
            alert = _raw_alert(item)
            alerts.append(alert)
        return alerts

    def _fetch(self) -> list[dict[str, Any]]:
        """GET списка алертов; ошибки сети и неожиданный ответ — StoreUnavailableError."""
        try:
            response = self._http.get(self._base_url + ALERTS_PATH, params=ACTIVE_ONLY)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise StoreUnavailableError(type(error).__name__) from error
        if response.status_code != HTTP_OK or not isinstance(body, list):
            raise StoreUnavailableError(f"HTTP {response.status_code}")
        return body


def _raw_alert(item: dict[str, Any]) -> RawAlert:
    """Алерт из элемента ответа Alertmanager."""
    alert = RawAlert(labels=item.get("labels") or {}, annotations=item.get("annotations") or {},
                     starts_at=item.get("startsAt"))
    return alert
