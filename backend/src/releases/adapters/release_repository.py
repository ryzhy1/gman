"""Хранение релизов (таблица releases)."""
from dataclasses import fields
from datetime import datetime

from sqlalchemy import select, tuple_

from database.keyset_cursor import InvalidCursorError as CursorFormatError
from database.keyset_cursor import decode_cursor, encode_cursor
from database.tables import ReleaseRecord
from database.unit_of_work import UnitOfWork
from releases.entity import OPEN_STATUSES, InvalidCursorError, Release, ReleaseFilter, ReleasePage

FIELDS = tuple(item.name for item in fields(Release))


class ReleaseRepository:
    """Чтение и запись релизов как сущностей."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def get(self, release_id: int) -> Release | None:
        """Релиз по ID или None."""
        record = self._unit_of_work.session.get(ReleaseRecord, release_id)
        if record is None:
            return None
        release = _release(record)
        return release

    def latest_by_key(self, service_id: str, environment: str, key: str) -> Release | None:
        """Последний релиз пары с ключом запуска Bamboo."""
        query = (select(ReleaseRecord).where(ReleaseRecord.service_id == service_id,
                                             ReleaseRecord.environment == environment, ReleaseRecord.key == key)
                 .order_by(ReleaseRecord.id.desc()).limit(1))
        record = self._unit_of_work.session.scalars(query).first()
        if record is None:
            return None
        release = _release(record)
        return release

    def open_for(self, service_id: str, environment: str) -> list[Release]:
        """Незавершенные релизы пары, новые первыми."""
        query = (select(ReleaseRecord).where(ReleaseRecord.service_id == service_id,
                                             ReleaseRecord.environment == environment,
                                             ReleaseRecord.status.in_(OPEN_STATUSES))
                 .order_by(ReleaseRecord.started_at.desc(), ReleaseRecord.id.desc()))
        releases = [_release(record) for record in self._unit_of_work.session.scalars(query)]
        return releases

    def stale_scopes(self, started_before: datetime) -> list[tuple[str, str]]:
        """Пары с незавершенными релизами, начатыми раньше `started_before`."""
        query = (select(ReleaseRecord.service_id, ReleaseRecord.environment)
                 .where(ReleaseRecord.status.in_(OPEN_STATUSES), ReleaseRecord.started_at < started_before)
                 .distinct())
        scopes = [(service_id, environment) for service_id, environment in self._unit_of_work.session.execute(query)]
        return scopes

    def add(self, release: Release) -> None:
        """Записывает новый релиз и присваивает ему ID."""
        record = ReleaseRecord()
        _copy(release, record)
        session = self._unit_of_work.session
        session.add(record)
        session.flush()
        release.id = record.id

    def save(self, release: Release) -> None:
        """Переносит изменения сущности в запись (запись уже в сессии — без запроса к БД)."""
        record = self._unit_of_work.session.get(ReleaseRecord, release.id)
        _copy(release, record)

    def page(self, release_filter: ReleaseFilter) -> ReleasePage:
        """Новые первыми по началу; `next` — keyset-курсор."""
        query = select(ReleaseRecord)
        for column, value in (("service_id", release_filter.service_id),
                              ("environment", release_filter.environment), ("status", release_filter.status)):
            if value is not None:
                query = query.where(getattr(ReleaseRecord, column) == value)
        if release_filter.before:
            query = query.where(tuple_(ReleaseRecord.started_at, ReleaseRecord.id) < _position(release_filter.before))
        query = query.order_by(ReleaseRecord.started_at.desc(), ReleaseRecord.id.desc()).limit(release_filter.limit + 1)
        records = self._unit_of_work.session.scalars(query).all()
        page_records = records[:release_filter.limit]
        releases = [_release(record) for record in page_records]
        has_more = len(records) > release_filter.limit
        next_cursor = encode_cursor(page_records[-1].started_at, page_records[-1].id) if has_more else None
        page = ReleasePage(releases=releases, next_cursor=next_cursor)
        return page


def _release(record: ReleaseRecord) -> Release:
    """Сущность релиза из записи."""
    values = {name: getattr(record, name) for name in FIELDS}
    values["progress"] = dict(values["progress"] or {})
    release = Release(**values)
    return release


def _copy(release: Release, record: ReleaseRecord) -> None:
    """Переносит изменившиеся поля сущности в запись (без лишних UPDATE)."""
    for name in FIELDS:
        value = getattr(release, name)
        if name == "id" and value is None:
            continue
        if getattr(record, name) != value:
            setattr(record, name, value)


def _position(before: str) -> tuple:
    """Позиция курсора; неразборчивый курсор — ошибка модуля."""
    try:
        position = decode_cursor(before)
    except CursorFormatError as error:
        raise InvalidCursorError(str(error)) from error
    return position
