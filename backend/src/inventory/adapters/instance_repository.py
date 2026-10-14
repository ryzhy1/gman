"""Хранение наблюдаемых экземпляров (таблица instances) и версий их конфигураций (config_snapshots)."""
from datetime import datetime

from sqlalchemy import select, update

from database.tables import ConfigSnapshotRecord, InstanceRecord
from database.unit_of_work import UnitOfWork
from inventory.entity import ObservedInstance

# Сколько ID обновляется одним UPDATE: ограничение числа параметров запроса.
REFRESH_BATCH = 5000


class InstanceRepository:
    """Экземпляры источника и версии конфигураций."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def current(self, source_id: str) -> dict[str, ObservedInstance]:
        """Экземпляры предыдущего снимка источника по ID."""
        query = select(InstanceRecord).where(InstanceRecord.source_id == source_id)
        instances = {}
        for record in self._unit_of_work.session.scalars(query):
            instances[record.instance_id] = ObservedInstance(document=record.data)
        return instances

    def replace(self, source_id: str, instances: list[ObservedInstance], config_hashes: list[str],
                previous_ids: set[str], received_at: datetime) -> None:
        """Заменяет экземпляры источника; новая конфигурация — новая версия, прежняя — продление ее срока.

        Записи предыдущего снимка уже в сессии (прочитаны current()), поэтому поиск по ключу не идет в БД.
        """
        session = self._unit_of_work.session
        unchanged = []
        for position, (instance, digest) in enumerate(zip(instances, config_hashes)):
            record = session.get(InstanceRecord, (source_id, instance.id))
            if record is None:
                record = InstanceRecord(source_id=source_id, instance_id=instance.id)
                session.add(record)
            if record.config_hash == digest and record.config_snapshot_id is not None:
                unchanged.append(record.config_snapshot_id)
            else:
                record.config_snapshot = _config_version(source_id, instance, digest, received_at)
            _fill(record, instance, position, digest)
        self._delete_gone(source_id, previous_ids - {instance.id for instance in instances})
        self._refresh(unchanged, received_at)

    def _delete_gone(self, source_id: str, gone_ids: set[str]) -> None:
        """Удаляет экземпляры, которых нет в новом снимке."""
        session = self._unit_of_work.session
        for instance_id in gone_ids:
            record = session.get(InstanceRecord, (source_id, instance_id))
            if record is not None:
                session.delete(record)

    def _refresh(self, version_ids: list[int], received_at: datetime) -> None:
        """Продлевает last_seen_at неизменившихся конфигураций пачками."""
        for start in range(0, len(version_ids), REFRESH_BATCH):
            batch = version_ids[start:start + REFRESH_BATCH]
            statement = (update(ConfigSnapshotRecord).where(ConfigSnapshotRecord.id.in_(batch))
                         .values(last_seen_at=received_at))
            self._unit_of_work.session.execute(statement)


def _config_version(source_id: str, instance: ObservedInstance, digest: str,
                    received_at: datetime) -> ConfigSnapshotRecord:
    """Новая версия конфигурации с подписями экземпляра на момент первого наблюдения."""
    version = ConfigSnapshotRecord(source_id=source_id, instance_id=instance.id, service_id=instance.service_id,
                                   instance_name=instance.name, environment=instance.environment,
                                   config_hash=digest, config=instance.config, first_seen_at=received_at,
                                   last_seen_at=received_at)
    return version


def _fill(record: InstanceRecord, instance: ObservedInstance, position: int, digest: str) -> None:
    """Переносит в запись документ экземпляра и его колонки фильтрации."""
    record.position = position
    record.config_hash = digest
    record.data = instance.document
    record.service_id = instance.service_id
    record.environment = instance.environment
    record.runtime = instance.document["runtime"]
    record.status = instance.document["status"]
