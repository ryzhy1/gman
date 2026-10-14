"""Очередь событий --watch: потоки событий пишут в нее из своих нитей, цикл ждет события или срока снимка."""
import queue
import threading
import time
from collections.abc import Callable

from watching.services.debounced_watcher import NOTHING

Feed = Callable[[queue.Queue], None]


class EventInbox:
    """События всех потоков в одной очереди."""

    def __init__(self, feeds: list[Feed], clock: Callable[[], float] = time.monotonic,
                 items: queue.Queue | None = None) -> None:
        """Запоминает потоки событий, часы и очередь (в тестах — заранее заполненную)."""
        self._feeds = feeds
        self._clock = clock
        self._items = items if items is not None else queue.Queue()

    def now(self) -> float:
        """Текущее время монотонных часов."""
        moment = self._clock()
        return moment

    def start(self) -> None:
        """Запускает потоки событий в фоновых нитях."""
        for feed in self._feeds:
            thread = threading.Thread(target=feed, args=(self._items,), daemon=True)
            thread.start()

    def next(self, deadline: float) -> object:
        """Следующее событие, None (поток прерывался) или NOTHING, если до срока событий не было."""
        try:
            item = self._items.get(timeout=max(0.0, deadline - self._clock()))
        except queue.Empty:
            return NOTHING
        return item
