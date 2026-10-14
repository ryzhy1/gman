"""Оркестратор модуля watching: снимки с debounce по потоку событий Docker или watch Kubernetes."""
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from watching.entity import JsonObject, SnapshotDocument
from watching.ports import SnapshotSource
from watching.services.debounced_watcher import DebouncedWatcher
from watching.services.event_inbox import EventInbox

logger = logging.getLogger(__name__)

COLLECTION_ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError)

Submit = Callable[[list[JsonObject]], object]


@dataclass(frozen=True)
class WatchingDependencies:
    """Готовые зависимости оркестратора watching."""

    watcher: DebouncedWatcher
    inbox: EventInbox
    source: SnapshotSource


class WatchingOrchestrator:
    """Единственная точка входа модуля watching."""

    def __init__(self, dependencies: WatchingDependencies) -> None:
        """Запоминает watcher, очередь событий и источник снимков."""
        self._watcher = dependencies.watcher
        self._inbox = dependencies.inbox
        self._source = dependencies.source

    def watch(self, submit: Submit, cycles: int | None = None) -> None:
        """Снимки по событиям: после затишья, не позже предела задержки и не реже интервала."""
        # Запускаем потоки событий.
        self._inbox.start()
        done = 0
        while cycles is None or done < cycles:
            # Ждем событие или срок снимка; разрыв потока — сверка полным снимком вне очереди.
            item = self._inbox.next(self._watcher.deadline())
            now = self._inbox.now()
            self._watcher.observe(item, now)
            if not self._watcher.due(now):
                continue
            # Собираем и отправляем снимок.
            done += 1
            self._snapshot(submit, now)

    def _snapshot(self, submit: Submit, now: float) -> None:
        """Сбор и отправка снимков одного цикла; неудачный сбор повторяется позже и не отправляется пустым."""
        seen = self._watcher.take(now)
        try:
            data = self._watcher.collect(self._source)
        except COLLECTION_ERRORS as error:
            logger.warning("collection failed: %s; retry in %gs", type(error).__name__, self._watcher.retry_delay)
            self._watcher.retry(seen, now, self._watcher.retry_delay)
            return
        # Добавляем эфемерные экземпляры, времена OOM и Events.
        collected = SnapshotDocument(data)
        snapshots = self._watcher.snapshots(collected, seen)
        # Отправляем через spool.
        submit([snapshot.document for snapshot in snapshots])
