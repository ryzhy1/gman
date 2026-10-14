"""Разбор времени из данных runtime: RFC 3339 c "Z" и наносекундами (Kubernetes, Docker)."""
import re
from datetime import datetime, timezone

UTC_SUFFIX = "Z"
UTC_OFFSET = "+00:00"
# Наносекунды и прочие длинные дроби: fromisoformat старых Python их не принимает.
LONG_FRACTION = re.compile(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)")
FRACTION_DIGITS = 7


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """Время как aware UTC; пусто или неразборчиво — None."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _parse_text(value)
    if parsed is None:
        return None
    aware = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return aware


def _parse_text(value: str) -> datetime | None:
    """Строка ISO 8601; длинная дробь секунды укорачивается."""
    try:
        parsed = datetime.fromisoformat(value.replace(UTC_SUFFIX, UTC_OFFSET))
    except ValueError:
        match = LONG_FRACTION.match(value)
        if not match:
            return None
        fraction = (match.group(2) or "")[:FRACTION_DIGITS]
        zone = match.group(3).replace(UTC_SUFFIX, UTC_OFFSET) or UTC_OFFSET
        parsed = datetime.fromisoformat(match.group(1) + fraction + zone)
    return parsed
