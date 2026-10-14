"""Хранение событий истории (таблица events): запись и постраничное чтение новых первыми."""
from sqlalchemy import select, tuple_

from database.keyset_cursor import InvalidCursorError as CursorFormatError
from database.keyset_cursor import decode_cursor, encode_cursor
from database.tables import EventRecord
from database.unit_of_work import UnitOfWork
from inventory.entity import HistoryEvent, HistoryPage, InvalidCursorError, JsonObject

CHANGES = "changes"


class EventRepository:
    """Аудит изменений: события inventory, каталога и деплоев."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def add_many(self, events: list[HistoryEvent]) -> list[int]:
        """Записывает события в переданном порядке и возвращает их ID."""
        session = self._unit_of_work.session
        records = []
        for event in events:
            record = _record(event)
            session.add(record)
            records.append(record)
        session.flush()
        ids = [record.id for record in records]
        return ids

    def page(self, limit: int, service_id: str | None, before: str) -> HistoryPage:
        """Новые первыми по времени приема; `next` — непрозрачный keyset-курсор."""
        query = select(EventRecord)
        if service_id is not None:
            query = query.where(EventRecord.service_id == service_id)
        if before:
            query = query.where(tuple_(EventRecord.received_at, EventRecord.id) < _position(before))
        query = query.order_by(EventRecord.received_at.desc(), EventRecord.id.desc()).limit(limit + 1)
        rows = self._unit_of_work.session.scalars(query).all()
        page_rows = rows[:limit]
        events = [_ordered(row.data) for row in page_rows]
        next_cursor = encode_cursor(page_rows[-1].received_at, page_rows[-1].id) if len(rows) > limit else None
        page = HistoryPage(events=events, next_cursor=next_cursor)
        return page


def _record(event: HistoryEvent) -> EventRecord:
    """Запись события; колонки фильтрации берутся из данных события."""
    data = event.data
    record = EventRecord(received_at=event.received_at, occurred_at=event.occurred_at, type=data["type"],
                         service_id=data.get("service_id", ""), source_id=data.get("source_id", ""),
                         instance_id=data.get("instance_id", ""), data=data)
    return record


def _position(before: str) -> tuple:
    """Позиция курсора; неразборчивый курсор — ошибка модуля."""
    try:
        position = decode_cursor(before)
    except CursorFormatError as error:
        raise InvalidCursorError(str(error)) from error
    return position


def _ordered(data: JsonObject) -> JsonObject:
    """JSONB не хранит порядок ключей: изменения отдаются отсортированными по имени поля."""
    if CHANGES not in data:
        return data
    ordered = {**data, CHANGES: dict(sorted(data[CHANGES].items()))}
    return ordered
