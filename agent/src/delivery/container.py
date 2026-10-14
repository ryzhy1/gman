"""Сборка модуля delivery: клиент API, spool, повторы и блокировка в каталоге состояния."""
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from config.settings import AgentSettings
from delivery.adapters.inventory_api import InventoryApi
from delivery.adapters.run_lock import RunLock
from delivery.adapters.spool_directory import SpoolDirectory
from delivery.entity import RetryPolicy, SpoolLimits
from delivery.orchestrator import DeliveryDependencies, DeliveryOrchestrator
from delivery.services.delivery_retry import DeliveryRetry
from delivery.services.run_guard import RunGuard
from delivery.services.snapshot_spool import SnapshotSpool

HTTP_SCHEMES = ("https://", "http://")
INVENTORY_PATH = "/api/inventory"
STATE_DIRECTORY_MODE = 0o700
RETRY_BASE_SECONDS = 2.0
RETRY_CAP_SECONDS = 30.0


@dataclass(frozen=True)
class DeliveryOptions:
    """Параметры доставки из командной строки."""

    source_id: str
    state_dir: Path
    spool_max_files: int
    spool_max_bytes: int
    retries: int


def inventory_endpoint(settings: AgentSettings) -> str:
    """Адрес приема снимков из GMAN_URL; проверяется до сбора чего-либо."""
    url = settings.gman_url.rstrip("/")
    if not url.startswith(HTTP_SCHEMES):
        raise ValueError("GMAN_URL must be an HTTP(S) URL")
    endpoint = url + INVENTORY_PATH
    return endpoint


def build_delivery_orchestrator(settings: AgentSettings, options: DeliveryOptions,
                                opener: Callable[..., Any] = urlopen, sleep: Callable[[float], None] = time.sleep,
                                rng: Callable[[], float] = random.random,
                                clock_ns: Callable[[], int] = time.time_ns) -> DeliveryOrchestrator:
    """Создает адаптеры, сервисы и оркестратор; каталог состояния создается только для владельца."""
    endpoint = inventory_endpoint(settings)
    options.state_dir.mkdir(mode=STATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    api = InventoryApi(endpoint, opener)
    policy = RetryPolicy(retries=options.retries, base_seconds=RETRY_BASE_SECONDS, cap_seconds=RETRY_CAP_SECONDS)
    retry = DeliveryRetry(api, policy, sleep, rng)
    spool_path = options.state_dir / f"spool-{options.source_id}"
    directory = SpoolDirectory(spool_path)
    limits = SpoolLimits(max_files=options.spool_max_files, max_bytes=options.spool_max_bytes)
    spool = SnapshotSpool(directory, limits, clock_ns)
    run_lock = RunLock()
    run_guard = RunGuard(run_lock, options.state_dir / f"{options.source_id}.lock")
    dependencies = DeliveryDependencies(run_guard=run_guard, spool=spool, retry=retry)
    orchestrator = DeliveryOrchestrator(dependencies)
    return orchestrator
