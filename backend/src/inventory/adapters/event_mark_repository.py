"""Отметки Events Kubernetes (таблица runtime_event_marks): дедупликация повторов и сводки по узлам."""
from datetime import datetime

from sqlalchemy import func, select

from database.column_types import as_utc
from database.tables import RuntimeEventMarkRecord
from database.unit_of_work import UnitOfWork
from inventory.entity import EventMark, NodeEventSummary


class EventMarkRepository:
    """Последние учтенные счетчик и время каждого Event."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def get(self, source_id: str, uid: str) -> EventMark | None:
        """Отметка Event или None, если он еще не записывался."""
        record = self._unit_of_work.session.get(RuntimeEventMarkRecord, (source_id, uid))
        if record is None:
            return None
        mark = EventMark(source_id=record.source_id, uid=record.uid, count=record.count, last_at=record.last_at,
                         received_at=record.received_at, node=record.node, reason=record.reason)
        return mark

    def save(self, mark: EventMark) -> None:
        """Создает или обновляет отметку."""
        session = self._unit_of_work.session
        record = session.get(RuntimeEventMarkRecord, (mark.source_id, mark.uid))
        if record is None:
            record = RuntimeEventMarkRecord(source_id=mark.source_id, uid=mark.uid, node=mark.node,
                                            reason=mark.reason)
            session.add(record)
        record.count = mark.count
        record.last_at = mark.last_at
        record.received_at = mark.received_at

    def node_summaries(self, since: datetime) -> dict[str, list[NodeEventSummary]]:
        """Events узлов, последний раз виденные после `since`, по источникам, новые первыми.

        `count` складывает счетчики Kubernetes (каждый Event — один объект, повторенный `count` раз).
        """
        mark = RuntimeEventMarkRecord
        query = (select(mark.source_id, mark.node, mark.reason, func.max(mark.last_at), func.sum(mark.count),
                        func.count()).where(mark.node != "", mark.last_at >= since)
                 .group_by(mark.source_id, mark.node, mark.reason))
        rows = sorted(self._unit_of_work.session.execute(query).all(), key=lambda row: as_utc(row[3]), reverse=True)
        summaries: dict[str, list[NodeEventSummary]] = {}
        for source_id, node, reason, last_at, count, events in rows:
            summary = NodeEventSummary(node=node, reason=reason, count=int(count), events=events,
                                       last_at=as_utc(last_at))
            summaries.setdefault(source_id, []).append(summary)
        return summaries
