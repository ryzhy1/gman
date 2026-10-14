"""События CI по ID (таблица deployments) для идемпотентных повторов."""
from datetime import datetime

from database.tables import DeploymentRecord
from database.unit_of_work import UnitOfWork
from releases.entity import JsonObject


class DeploymentRepository:
    """Принятые запросы событий CI."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def get(self, event_id: str) -> JsonObject | None:
        """Сохраненный запрос события или None."""
        record = self._unit_of_work.session.get(DeploymentRecord, event_id)
        if record is None:
            return None
        request = record.request
        return request

    def add(self, event_id: str, request: JsonObject, received_at: datetime) -> None:
        """Сохраняет запрос события."""
        record = DeploymentRecord(event_id=event_id, request=request, received_at=received_at)
        self._unit_of_work.session.add(record)
