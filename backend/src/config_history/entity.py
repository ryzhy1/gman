"""Сущности модуля config_history: версии конфигураций экземпляров и изменения между ними."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any

JsonObject = dict[str, Any]


class ConfigHistoryError(Exception):
    """Базовая ошибка модуля config_history."""


class UnknownServiceError(ConfigHistoryError):
    """Сервис не известен ни каталогу, ни истории конфигураций."""


class UnknownInstanceError(ConfigHistoryError):
    """Экземпляр не наблюдается и не встречается в истории конфигураций."""


class UnknownVersionError(ConfigHistoryError):
    """Версии конфигурации с таким ID нет."""

    def __init__(self, version_id: int) -> None:
        """Запоминает ID отсутствующей версии."""
        super().__init__(f"Config version {version_id} not found")
        self.version_id = version_id


class InvalidCursorError(ConfigHistoryError):
    """Курсор страницы не удалось разобрать."""


@dataclass(frozen=True)
class VersionScope:
    """Чьи версии читать: сервиса или одного экземпляра (source_id + instance_id)."""

    service_id: str | None = None
    source_id: str | None = None
    instance_id: str | None = None


@dataclass(frozen=True)
class ConfigVersion:
    """Отличающаяся конфигурация экземпляра с интервалом ее наблюдения."""

    id: int
    service_id: str
    source_id: str
    instance_id: str
    instance_name: str
    environment: str
    config_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    current: bool
    config: JsonObject | None = None


@dataclass(frozen=True)
class VersionPage:
    """Страница версий (новые первыми) и курсор следующей страницы."""

    versions: list[ConfigVersion]
    next_cursor: str | None


@dataclass(frozen=True)
class ConfigChange:
    """Одно изменение: путь, операция (added/removed/changed) и значения до и после."""

    path: str
    op: str
    before: Any = None
    after: Any = None


@dataclass(frozen=True)
class ConfigComparison:
    """Сравнение двух версий."""

    older: ConfigVersion
    newer: ConfigVersion
    changes: list[ConfigChange]
    summary: dict[str, int]

    @property
    def identical(self) -> bool:
        """Совпадают ли конфигурации по хешу."""
        same = self.older.config_hash == self.newer.config_hash
        return same
