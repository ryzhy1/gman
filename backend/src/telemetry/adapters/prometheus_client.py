"""Адаптер HTTP API Prometheus (только чтение): мгновенные запросы и ряды за период."""
import math
from typing import Any

import httpx

from telemetry.entity import Sample, SeriesPoint, StoreUnavailableError

INSTANT_PATH = "/api/v1/query"
RANGE_PATH = "/api/v1/query_range"
SUCCESS_STATUS = "success"
HTTP_OK = 200
ERROR_DETAIL_LIMIT = 200


class PrometheusClient:
    """Выполняет готовые PromQL-выражения и возвращает выборки как сущности."""

    def __init__(self, base_url: str, http_client: httpx.Client) -> None:
        """Запоминает адрес Prometheus и HTTP-клиент (таймауты задает контейнер)."""
        self._base_url = base_url
        self._http = http_client

    @property
    def enabled(self) -> bool:
        """Задан ли адрес Prometheus."""
        configured = bool(self._base_url)
        return configured

    def instant(self, promql: str) -> list[Sample]:
        """Последние значения выражения."""
        result = self._request(INSTANT_PATH, {"query": promql})
        samples = []
        for item in result:
            value = _number(item["value"][1])
            sample = Sample(labels=item["metric"], value=value)
            samples.append(sample)
        return samples

    def first_series(self, promql: str, start: float, end: float, step: int) -> list[SeriesPoint]:
        """Точки первого ряда выражения за период; пустой список, если рядов нет."""
        params = {"query": promql, "start": start, "end": end, "step": step}
        result = self._request(RANGE_PATH, params)
        raw_points = result[0]["values"] if result else []
        points = []
        for timestamp, raw in raw_points:
            point = SeriesPoint(timestamp=float(timestamp), value=_number(raw))
            points.append(point)
        return points

    def _request(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """GET к API Prometheus; сетевые ошибки и ответы не 200/success — StoreUnavailableError."""
        try:
            response = self._http.get(self._base_url + path, params=params)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise StoreUnavailableError(type(error).__name__) from error
        if response.status_code != HTTP_OK or not isinstance(body, dict) or body.get("status") != SUCCESS_STATUS:
            detail = str(body.get("error", "") if isinstance(body, dict) else "")[:ERROR_DETAIL_LIMIT]
            raise StoreUnavailableError(f"HTTP {response.status_code}: {detail}")
        result = body["data"]["result"]
        return result


def _number(raw: str) -> float | None:
    """Значение выборки; NaN и бесконечности — отсутствие значения, а не число."""
    value = float(raw)
    if math.isnan(value) or math.isinf(value):
        return None
    return value
