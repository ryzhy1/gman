"""Чтение версий конфигураций из БД (таблицы config_snapshots, instances, services)."""
from sqlalchemy import Select, select, tuple_

from config_history.entity import ConfigVersion, InvalidCursorError, VersionPage, VersionScope
from database.keyset_cursor import InvalidCursorError as CursorFormatError
from database.keyset_cursor import decode_cursor, encode_cursor
from database.tables import ConfigSnapshotRecord, InstanceRecord, ServiceRecord
from database.unit_of_work import UnitOfWork


class ConfigVersionRepository:
    """Версии конфигураций и проверки существования сервиса и экземпляра."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def service_known(self, service_id: str) -> bool:
        """Есть ли сервис в каталоге или хотя бы одна его версия конфигурации."""
        session = self._unit_of_work.session
        if session.get(ServiceRecord, service_id) is not None:
            return True
        query = select(ConfigSnapshotRecord.id).where(ConfigSnapshotRecord.service_id == service_id).limit(1)
        known = session.scalar(query) is not None
        return known

    def instance_known(self, source_id: str, instance_id: str) -> bool:
        """Наблюдается ли экземпляр сейчас или встречается ли в истории конфигураций."""
        session = self._unit_of_work.session
        if session.get(InstanceRecord, (source_id, instance_id)) is not None:
            return True
        query = select(ConfigSnapshotRecord.id).where(ConfigSnapshotRecord.source_id == source_id,
                                                      ConfigSnapshotRecord.instance_id == instance_id).limit(1)
        known = session.scalar(query) is not None
        return known

    def page(self, scope: VersionScope, limit: int, before: str) -> VersionPage:
        """Версии области, новые первыми по первому наблюдению; курсор — keyset."""
        query = _scoped(select(ConfigSnapshotRecord), scope)
        if before:
            query = query.where(tuple_(ConfigSnapshotRecord.first_seen_at, ConfigSnapshotRecord.id)
                                < _position(before))
        query = query.order_by(ConfigSnapshotRecord.first_seen_at.desc(), ConfigSnapshotRecord.id.desc())
        rows = self._unit_of_work.session.scalars(query.limit(limit + 1)).all()
        page_rows = rows[:limit]
        current = self._current_ids([row.id for row in page_rows])
        versions = [_version(row, row.id in current, with_config=False) for row in page_rows]
        has_more = len(rows) > limit
        next_cursor = encode_cursor(page_rows[-1].first_seen_at, page_rows[-1].id) if has_more else None
        page = VersionPage(versions=versions, next_cursor=next_cursor)
        return page

    def get(self, version_id: int) -> ConfigVersion | None:
        """Версия с телом конфигурации или None."""
        row = self._unit_of_work.session.get(ConfigSnapshotRecord, version_id)
        if row is None:
            return None
        current = self._current_ids([row.id])
        version = _version(row, row.id in current, with_config=True)
        return version

    def _current_ids(self, ids: list[int]) -> set[int]:
        """ID из `ids`, которые запущены у какого-либо наблюдаемого сейчас экземпляра."""
        current: set[int] = set()
        if not ids:
            return current
        query = select(InstanceRecord.config_snapshot_id).where(InstanceRecord.config_snapshot_id.in_(ids))
        current.update(self._unit_of_work.session.scalars(query))
        return current


def _scoped(query: Select, scope: VersionScope) -> Select:
    """Ограничивает выборку сервисом или экземпляром."""
    if scope.service_id is not None:
        query = query.where(ConfigSnapshotRecord.service_id == scope.service_id)
    if scope.source_id is not None:
        query = query.where(ConfigSnapshotRecord.source_id == scope.source_id,
                            ConfigSnapshotRecord.instance_id == scope.instance_id)
    return query


def _position(before: str) -> tuple:
    """Позиция keyset-курсора; неразборчивый курсор — ошибка модуля."""
    try:
        position = decode_cursor(before)
    except CursorFormatError as error:
        raise InvalidCursorError(str(error)) from error
    return position


def _version(row: ConfigSnapshotRecord, current: bool, with_config: bool) -> ConfigVersion:
    """Сущность версии из записи."""
    version = ConfigVersion(id=row.id, service_id=row.service_id, source_id=row.source_id,
                            instance_id=row.instance_id, instance_name=row.instance_name,
                            environment=row.environment, config_hash=row.config_hash,
                            first_seen_at=row.first_seen_at, last_seen_at=row.last_seen_at, current=current,
                            config=row.config if with_config else None)
    return version
