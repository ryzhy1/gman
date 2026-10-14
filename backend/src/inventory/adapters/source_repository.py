"""Хранение последних снимков источников (таблица sources) и чтение их вместе с экземплярами."""
from datetime import datetime

from sqlalchemy import select

from database.tables import InstanceRecord, SourceRecord
from database.unit_of_work import UnitOfWork
from inventory.entity import JsonObject, SnapshotReport


class SourceRepository:
    """Последнее состояние каждого источника."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def observed_at(self, source_id: str) -> datetime | None:
        """Время наблюдения последнего принятого снимка источника или None."""
        record = self._unit_of_work.session.get(SourceRecord, source_id)
        if record is None:
            return None
        observed_at = record.observed_at
        return observed_at

    def save(self, report: SnapshotReport, received_at: datetime) -> None:
        """Заменяет последнее состояние источника."""
        record = SourceRecord(source_id=report.source_id, source_type=report.source_type, hostname=report.hostname,
                              observed_at=report.observed_at, received_at=received_at,
                              snapshot=report.source_document)
        self._unit_of_work.session.merge(record)

    def documents_with_instances(self) -> list[JsonObject]:
        """Документы источников с экземплярами в порядке снимка и временем приема."""
        session = self._unit_of_work.session
        instances: dict[str, list[JsonObject]] = {}
        query = select(InstanceRecord).order_by(InstanceRecord.source_id, InstanceRecord.position)
        for instance in session.scalars(query):
            instances.setdefault(instance.source_id, []).append(instance.data)
        documents = []
        for record in session.scalars(select(SourceRecord).order_by(SourceRecord.source_id)):
            document = {**record.snapshot, "instances": instances.get(record.source_id, []),
                        "received_at": record.received_at.isoformat()}
            documents.append(document)
        return documents
