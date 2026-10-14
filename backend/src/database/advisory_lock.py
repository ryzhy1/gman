"""Транзакционные advisory-блокировки PostgreSQL для сериализации конкурентных записей по ключу."""
import hashlib

from sqlalchemy import text

from database.column_types import POSTGRESQL
from database.unit_of_work import UnitOfWork

LOCK_ID_BYTES = 8
LOCK_ID_BYTE_ORDER = "big"
LOCK_STATEMENT = text("SELECT pg_advisory_xact_lock(:id)")


class AdvisoryLock:
    """Блокировка по строковому ключу до конца текущей транзакции."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, в которой берутся блокировки."""
        self._unit_of_work = unit_of_work

    def acquire(self, name: str) -> None:
        """Ждет и берет блокировку ключа; на SQLite (только тесты) ничего не делает."""
        if self._unit_of_work.dialect_name != POSTGRESQL:
            return
        digest = hashlib.sha256(name.encode()).digest()[:LOCK_ID_BYTES]
        lock_id = int.from_bytes(digest, LOCK_ID_BYTE_ORDER, signed=True)
        self._unit_of_work.session.execute(LOCK_STATEMENT, {"id": lock_id})
