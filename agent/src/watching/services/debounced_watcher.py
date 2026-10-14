"""Debounce снимков в режиме --watch: когда пора собирать снимок по потоку событий."""
from watching.entity import JsonObject, SnapshotDocument, WatchTimings

# Пустой элемент очереди: срок ожидания истек без событий.
NOTHING = object()


class DebouncedWatcher:
    """Снимок — после debounce секунд тишины, не позже max_delay после первого события и не реже interval.

    Экземпляры, увиденные в событиях, хранятся до следующего снимка, чтобы короткоживущие тоже попали в него.
    """

    def __init__(self, now: float, timings: WatchTimings) -> None:
        """Первый полный снимок — сразу после старта."""
        self._timings = timings
        self._first: float | None = None
        self._last: float | None = None
        self._next_poll = now
        self._seen: dict[str, JsonObject] = {}

    @property
    def seen(self) -> dict[str, JsonObject]:
        """Экземпляры, увиденные в событиях с прошлого снимка."""
        seen = self._seen
        return seen

    @property
    def retry_delay(self) -> float:
        """Через сколько повторить неудавшийся сбор."""
        delay = self._timings.retry_delay
        return delay

    def observe(self, item: object, now: float) -> None:
        """Элемент очереди: None — поток прерывался (события могли потеряться), иначе событие."""
        if item is None:
            self.resync(now)
        elif item is not NOTHING:
            self.handle(item, now)

    def handle(self, item: object, now: float) -> None:
        """Событие потока; определяют наследники."""
        raise NotImplementedError

    def snapshots(self, snapshot: SnapshotDocument, seen: dict[str, JsonObject]) -> list[SnapshotDocument]:
        """Снимки для отправки по одному сбору; определяют наследники."""
        raise NotImplementedError

    def trigger(self, now: float) -> None:
        """Событие, меняющее снимок: запускает отсчет debounce."""
        self._last = now
        if self._first is None:
            self._first = now

    def resync(self, now: float) -> None:
        """Снимок как можно скорее."""
        self._next_poll = min(self._next_poll, now)

    def deadline(self) -> float:
        """Когда собирать следующий снимок."""
        if self._last is None:
            return self._next_poll
        deadline = min(self._next_poll, self._last + self._timings.debounce, self._first + self._timings.max_delay)
        return deadline

    def due(self, now: float) -> bool:
        """Пора ли собирать снимок."""
        due = now >= self.deadline()
        return due

    def take(self, now: float) -> dict[str, JsonObject]:
        """Начинает снимок: отдает увиденные экземпляры и назначает следующий плановый снимок."""
        seen, self._seen = self._seen, {}
        self._first = self._last = None
        self._next_poll = now + self._timings.interval
        return seen

    def retry(self, seen: dict[str, JsonObject], now: float, delay: float) -> None:
        """Сбор не удался: увиденные экземпляры возвращаются, снимок повторяется через delay."""
        self._seen = {**seen, **self._seen}
        self._next_poll = min(self._next_poll, now + delay)
