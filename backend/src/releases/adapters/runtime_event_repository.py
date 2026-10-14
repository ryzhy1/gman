"""События истории (таблица events) с точки зрения релизов: запись деплоя, выборка и связывание."""
from datetime import datetime

from sqlalchemy import select

from database.tables import EventRecord
from database.unit_of_work import UnitOfWork
from releases.adapters.rollout_instance_repository import rollout_instance
from releases.entity import RUNTIME_TYPES, JsonObject, RuntimeEvent

CHANGES = "changes"


class RuntimeEventRepository:
    """Записи истории, которые связываются с релизами."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def add(self, data: JsonObject, received_at: datetime, occurred_at: datetime) -> int:
        """Записывает событие и возвращает его ID."""
        record = EventRecord(received_at=received_at, occurred_at=occurred_at, type=data["type"],
                             service_id=data.get("service_id", ""), source_id=data.get("source_id", ""),
                             instance_id=data.get("instance_id", ""), data=data)
        session = self._unit_of_work.session
        session.add(record)
        session.flush()
        event_id = record.id
        return event_id

    def by_ids(self, event_ids: list[int], service_id: str) -> list[RuntimeEvent]:
        """События сервиса среди только что записанных (по ID)."""
        if not event_ids:
            return []
        query = (select(EventRecord).where(EventRecord.id.in_(event_ids), EventRecord.service_id == service_id)
                 .order_by(EventRecord.id))
        events = [_event(record) for record in self._unit_of_work.session.scalars(query)]
        return events

    def uncorrelated(self, service_id: str, start: datetime, end: datetime) -> list[RuntimeEvent]:
        """Несвязанные изменения runtime сервиса, случившиеся в окне."""
        query = (select(EventRecord)
                 .where(EventRecord.service_id == service_id, EventRecord.release_id.is_(None),
                        EventRecord.type.in_(RUNTIME_TYPES), EventRecord.occurred_at >= start,
                        EventRecord.occurred_at <= end)
                 .order_by(EventRecord.id))
        events = [_event(record) for record in self._unit_of_work.session.scalars(query)]
        return events

    def save_links(self, events: list[RuntimeEvent]) -> None:
        """Сохраняет связь событий с релизом и измененные данные."""
        session = self._unit_of_work.session
        for event in events:
            record = session.get(EventRecord, event.id)
            record.data = event.data
            record.release_id = event.release_id

    def link(self, event_id: int, release_id: int) -> None:
        """Связывает запись истории с релизом (ID дублируется в данных)."""
        record = self._unit_of_work.session.get(EventRecord, event_id)
        record.release_id = release_id
        record.data = {**record.data, "release_id": release_id}

    def linked_to(self, release_id: int, limit: int) -> list[JsonObject]:
        """Данные событий релиза, новые первыми."""
        query = (select(EventRecord).where(EventRecord.release_id == release_id)
                 .order_by(EventRecord.received_at.desc(), EventRecord.id.desc()).limit(limit))
        events = [_ordered(record.data) for record in self._unit_of_work.session.scalars(query)]
        return events


def _event(record: EventRecord) -> RuntimeEvent:
    """Сущность события из записи; экземпляры до и после — из данных события."""
    before, after = record.data.get("before"), record.data.get("after")
    event = RuntimeEvent(id=record.id, type=record.type, service_id=record.service_id,
                         occurred_at=record.occurred_at, release_id=record.release_id, data=record.data,
                         before=rollout_instance(record.source_id, before) if before else None,
                         after=rollout_instance(record.source_id, after) if after else None)
    return event


def _ordered(data: JsonObject) -> JsonObject:
    """JSONB не хранит порядок ключей: изменения отдаются отсортированными по имени поля."""
    if CHANGES not in data:
        return data
    ordered = {**data, CHANGES: dict(sorted(data[CHANGES].items()))}
    return ordered
