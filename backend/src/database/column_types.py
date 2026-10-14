"""Типы колонок, общие для PostgreSQL (основная СУБД) и SQLite (только тесты и локальная разработка)."""
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, Dialect, Integer, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB

POSTGRESQL = "postgresql"
SQLITE = "sqlite"

POSTGRESQL_JSON = JSONB()
JSON_DOCUMENT = JSON().with_variant(POSTGRESQL_JSON, POSTGRESQL)
# SQLite автоматически увеличивает только ключи типа INTEGER.
SQLITE_ROW_ID = Integer()
ROW_ID = BigInteger().with_variant(SQLITE_ROW_ID, SQLITE)


def as_utc(value: datetime | str) -> datetime:
    """Приводит время из агрегатов (max) к aware UTC, что бы ни вернул драйвер."""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        aware = parsed.replace(tzinfo=timezone.utc)
    else:
        aware = parsed.astimezone(timezone.utc)
    return aware


def utc_now() -> datetime:
    """Текущее время в UTC."""
    now = datetime.now(timezone.utc)
    return now


class UtcDateTime(TypeDecorator):
    """timestamptz на PostgreSQL; SQLite хранит наивное UTC, поэтому зона восстанавливается при чтении."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Переводит время в UTC перед записью; для SQLite убирает зону."""
        if value is None:
            return None
        utc_value = value.astimezone(timezone.utc)
        if dialect.name == SQLITE:
            utc_value = utc_value.replace(tzinfo=None)
        return utc_value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Возвращает прочитанное время как aware UTC."""
        if value is None:
            return None
        aware = as_utc(value)
        return aware
