"""Протокол list + watch Kubernetes для одного ресурса: элементы потока в очередь --watch.

После каждого (повторного) list в очередь идет SYNC со всеми объектами, затем ADDED/MODIFIED/DELETED по одному.
BOOKMARK только сдвигает resourceVersion. Watch, завершенный сервером по timeoutSeconds, продолжается с последней
версии; 410 Gone (устаревшая версия) — сразу новый list; другие сбои кладут None (сверка) и повторяют list после
экспоненциальной паузы.
"""
import logging
import queue
import random
import subprocess
import time
from collections.abc import Callable
from urllib.parse import quote

from runtime_access.kubernetes_api import JsonObject, KubernetesApi, KubernetesGoneError
from watching.entity import CHANGES, SYNC, KubeObject, WatchItem

logger = logging.getLogger(__name__)

WATCH_TIMEOUT_SECONDS = 300
GONE = 410
ERROR = "ERROR"
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
HALF = 2
FEED_ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError)


class KubernetesWatchFeed:
    """Поток одного ресурса (pod-ы, Events pod-ов или Events узлов)."""

    def __init__(self, api: KubernetesApi, resource: str, api_path: str, timeout: int = WATCH_TIMEOUT_SECONDS,
                 sleep: Callable[[float], None] = time.sleep, rng: Callable[[], float] = random.random) -> None:
        """Запоминает API, имя ресурса в очереди, путь API, тайм-аут watch, ожидание и генератор джиттера."""
        self._api = api
        self._resource = resource
        self._api_path = api_path
        self._timeout = timeout
        self._sleep = sleep
        self._rng = rng
        # Сбои подряд: каждое полученное событие сбрасывает счетчик.
        self._failures = 0

    def run(self, sink: queue.Queue, attempts: int | None = None) -> None:
        """Список, затем watch с версии списка; attempts ограничивает число циклов в тестах."""
        version = None
        while attempts is None or attempts > 0:
            if attempts is not None:
                attempts -= 1
            try:
                version = self._cycle(sink, version)
            except KubernetesGoneError:
                logger.info("%s watch: resourceVersion expired (410 Gone); relisting", self._resource)
                version = None
            except FEED_ERRORS as error:
                delay = _reconnect_delay(self._failures, self._rng)
                self._failures += 1
                logger.warning("%s watch failed: %s; relist in %.1fs", self._resource, type(error).__name__, delay)
                version = None
                sink.put(None)
                self._sleep(delay)

    def _cycle(self, sink: queue.Queue, version: str | None) -> str:
        """List (если версии нет) и один watch; возвращает последнюю увиденную версию."""
        if version is None:
            listing = self._api.raw(self._api_path)
            version = (listing.get("metadata") or {})["resourceVersion"]
            objects = [_kube_object(item) for item in listing.get("items") or []]
            sync = WatchItem(resource=self._resource, kind=SYNC, objects=objects)
            sink.put(sync)
        for event in self._api.stream(watch_path(self._api_path, version, self._timeout), self._timeout):
            version = self._handle(sink, event, version)
        return version

    def _handle(self, sink: queue.Queue, event: JsonObject, version: str) -> str:
        """Событие watch: ошибка Status, изменение объекта или bookmark; возвращает новую версию."""
        kind, obj = event.get("type"), event.get("object") or {}
        if kind == ERROR:
            # API сообщает устаревшую версию объектом Status внутри потока.
            if obj.get("code") == GONE:
                raise KubernetesGoneError()
            raise RuntimeError(f"watch error {obj.get('code')} {obj.get('reason', '')}")
        version = (obj.get("metadata") or {}).get("resourceVersion") or version
        self._failures = 0
        if kind in CHANGES:
            change = WatchItem(resource=self._resource, kind=kind, objects=[_kube_object(obj)])
            sink.put(change)
        return version


def watch_path(api_path: str, version: str, timeout: int) -> str:
    """Путь watch с закладками, версией и тайм-аутом сервера."""
    separator = "&" if "?" in api_path else "?"
    path = (f"{api_path}{separator}watch=1&allowWatchBookmarks=true&resourceVersion={quote(str(version))}"
            f"&timeoutSeconds={int(timeout)}")
    return path


def _kube_object(raw: JsonObject) -> KubeObject:
    """Объект потока с UID из metadata."""
    kube_object = KubeObject(uid=(raw.get("metadata") or {}).get("uid") or "", raw=raw)
    return kube_object


def _reconnect_delay(failures: int, rng: Callable[[], float]) -> float:
    """Пауза перед новым list: экспоненциальная, не меньше половины шага."""
    step = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** failures)
    delay = step / HALF + rng() * step / HALF
    return delay
