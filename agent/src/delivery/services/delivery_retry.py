"""Доставка снимка с повторами временных сбоев: экспоненциальный backoff с «равным» джиттером."""
import logging
import random
import time
from collections.abc import Callable

from delivery.adapters.inventory_api import InventoryApi
from delivery.entity import JsonObject, RetryableError, RetryPolicy

logger = logging.getLogger(__name__)

HALF = 2


class DeliveryRetry:
    """Повторяет только временные сбои; окончательный отказ и блокировка отдаются сразу."""

    def __init__(self, api: InventoryApi, policy: RetryPolicy, sleep: Callable[[float], None] = time.sleep,
                 rng: Callable[[], float] = random.random) -> None:
        """Запоминает клиент API, политику повторов, ожидание и генератор джиттера."""
        self._api = api
        self._policy = policy
        self._sleep = sleep
        self._rng = rng

    def deliver(self, snapshot: JsonObject) -> None:
        """Отправляет снимок; после исчерпания повторов — RetryableError."""
        for attempt in range(self._policy.retries + 1):
            try:
                self._api.post(snapshot)
                return
            except RetryableError as error:
                if attempt == self._policy.retries:
                    raise
                delay = backoff(attempt, self._policy.base_seconds, self._policy.cap_seconds, self._rng)
                logger.warning("delivery failed (%s); retry %s/%s in %.1fs", error, attempt + 1,
                               self._policy.retries, delay)
                self._sleep(delay)


def backoff(attempt: int, base: float, cap: float, rng: Callable[[], float] = random.random) -> float:
    """Задержка попытки: не меньше половины шага, поэтому задержки все равно растут."""
    step = min(cap, base * 2 ** attempt)
    delay = step / HALF + rng() * step / HALF
    return delay
