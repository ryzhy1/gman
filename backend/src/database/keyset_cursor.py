"""Непрозрачный keyset-курсор страниц: время сортировки и ID последней записи страницы."""
import base64
import binascii
from datetime import datetime

CURSOR_SEPARATOR = "|"
BASE64_BLOCK = 4
BASE64_PADDING = "="


class InvalidCursorError(ValueError):
    """Курсор не удалось разобрать."""


def encode_cursor(at: datetime, row_id: int) -> str:
    """Кодирует позицию страницы в строку для параметра `before`."""
    raw = f"{at.isoformat()}{CURSOR_SEPARATOR}{row_id}"
    encoded = base64.urlsafe_b64encode(raw.encode()).decode().rstrip(BASE64_PADDING)
    return encoded


def decode_cursor(value: str) -> tuple[datetime, int]:
    """Разбирает курсор; любая ошибка формата — InvalidCursorError."""
    padding = BASE64_PADDING * (-len(value) % BASE64_BLOCK)
    try:
        raw = base64.urlsafe_b64decode(value + padding).decode()
        at_text, _, row_id = raw.partition(CURSOR_SEPARATOR)
        at = datetime.fromisoformat(at_text)
        position = (at, int(row_id))
    except (ValueError, binascii.Error) as error:
        raise InvalidCursorError("Invalid cursor") from error
    if at.tzinfo is None:
        raise InvalidCursorError("Invalid cursor")
    return position
