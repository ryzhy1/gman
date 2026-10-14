"""Удаление истории старше срока хранения."""
from datetime import datetime

from sqlalchemy import delete

from database.tables import ConfigSnapshotRecord, EventRecord, ReleaseRecord, RuntimeEventMarkRecord
from database.unit_of_work import UnitOfWork


class RetentionPurger:
    """Удаляет события, отметки Events, неактуальные версии конфигураций и завершенные релизы."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции."""
        self._unit_of_work = unit_of_work

    def purge(self, cutoff: datetime) -> None:
        """Удаляет записи старше `cutoff` в отдельной транзакции."""
        with self._unit_of_work.transaction():
            session = self._unit_of_work.session
            session.execute(delete(EventRecord).where(EventRecord.received_at < cutoff))
            # Kubernetes по умолчанию хранит Events час: более старые отметки уже ничего не дедуплицируют.
            session.execute(delete(RuntimeEventMarkRecord).where(RuntimeEventMarkRecord.received_at < cutoff))
            # Текущие конфигурации остаются: каждый принятый снимок продлевает их last_seen_at.
            session.execute(delete(ConfigSnapshotRecord).where(ConfigSnapshotRecord.last_seen_at < cutoff))
            session.execute(delete(ReleaseRecord).where(ReleaseRecord.started_at < cutoff,
                                                        ReleaseRecord.finished_at.is_not(None)))
