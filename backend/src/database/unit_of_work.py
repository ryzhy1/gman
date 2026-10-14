"""Граница транзакции use-case: одна сессия SQLAlchemy на сценарий, общая для всех его репозиториев."""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


class StorageError(Exception):
    """Ошибка хранилища внутри транзакции (обертка над ошибками SQLAlchemy)."""


class UnitOfWork:
    """Открывает транзакцию сценария и отдает ее сессию репозиториям.

    Сценарий другого модуля, вызванный внутри уже открытой транзакции (например, учет релизов при приеме
    снимка), присоединяется к ней: все изменения фиксируются одним commit.
    """

    def __init__(self, engine: Engine) -> None:
        """Запоминает engine; сессия создается на каждую транзакцию."""
        self._engine = engine
        self._current: ContextVar[Session | None] = ContextVar(f"gman_session_{id(self)}", default=None)

    @property
    def dialect_name(self) -> str:
        """Имя диалекта СУБД (postgresql или sqlite)."""
        name = self._engine.dialect.name
        return name

    @property
    def session(self) -> Session:
        """Сессия текущей транзакции; вне транзакции — ошибка программы."""
        session = self._current.get()
        if session is None:
            raise RuntimeError("Repository used outside of a use-case transaction")
        return session

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Открывает транзакцию (commit при успехе) или присоединяется к уже открытой."""
        if self._current.get() is not None:
            yield
            return
        with Session(self._engine) as session:
            token = self._current.set(session)
            try:
                yield
                session.commit()
            except SQLAlchemyError as error:
                raise StorageError("Database transaction failed") from error
            finally:
                self._current.reset(token)
