"""Журнал истории: запись событий и постраничное чтение аудита."""
from inventory.adapters.event_repository import EventRepository
from inventory.entity import HistoryEvent, HistoryPage


class HistoryJournal:
    """Аудит изменений inventory и каталога."""

    def __init__(self, events: EventRepository) -> None:
        """Запоминает репозиторий событий."""
        self._events = events

    def record(self, events: list[HistoryEvent]) -> list[int]:
        """Записывает события по порядку; возвращает их ID (нужны для связи с релизами)."""
        if not events:
            return []
        ids = self._events.add_many(events)
        return ids

    def page(self, limit: int, service_id: str | None = None, before: str = "") -> HistoryPage:
        """Страница аудита, новые первыми; `before` — курсор `next` предыдущей страницы."""
        page = self._events.page(limit, service_id, before)
        return page
