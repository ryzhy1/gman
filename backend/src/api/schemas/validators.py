"""Общие проверки входных данных HTTP-моделей.

Модуль самодостаточен (только pydantic и stdlib): схемы API загружаются и тестами агента для проверки контракта.
"""
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

HTTP_SCHEMES = ("http", "https")
# Допустимое расхождение часов агента и сервера.
MAX_FUTURE_SECONDS = 300


class Strict(BaseModel):
    """Модель без лишних полей: неизвестное поле — ошибка 422, а не молча сохраненные данные."""

    model_config = ConfigDict(extra="forbid")


def http_url(value: str) -> str:
    """Пустая строка или HTTP(S)-адрес без учетных данных."""
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in HTTP_SCHEMES or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) URL without credentials")
    return value


def not_future(value: datetime) -> datetime:
    """Время с часовым поясом и не из будущего (с запасом на расхождение часов)."""
    if value.tzinfo is None:
        raise ValueError("Timezone required")
    if (value - datetime.now(timezone.utc)).total_seconds() > MAX_FUTURE_SECONDS:
        raise ValueError("Timestamp is in the future")
    return value


def optional_not_future(value: datetime | None) -> datetime | None:
    """Как not_future, но время может отсутствовать."""
    if value is None:
        return None
    checked = not_future(value)
    return checked
