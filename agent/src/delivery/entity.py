"""Сущности модуля delivery: исход доставки, лимиты spool и снимок в spool."""
from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]
ERRORS_KEPT = 49


class RetryableError(Exception):
    """Временный сбой (сеть, тайм-аут, 5xx, 429): снимок остается и отправляется позже."""


class BlockedError(Exception):
    """API или прокси перед ним отвергают агента (401/403): повторять сейчас бессмысленно, снимок остается."""


class RejectedError(Exception):
    """API окончательно отвергает снимок (например, 422): он удаляется, чтобы не блокировать spool."""


class SubmissionPendingError(Exception):
    """Не все снимки доставлены: они остались в spool и будут отправлены следующим запуском."""

    def __init__(self, remaining: int) -> None:
        """Запоминает число неотправленных снимков."""
        super().__init__(f"Submission failed: {remaining} snapshot(s) kept in spool for retry")
        self.remaining = remaining


@dataclass(frozen=True)
class SpoolLimits:
    """Пределы spool по числу файлов и суммарному размеру."""

    max_files: int
    max_bytes: int


@dataclass(frozen=True)
class RetryPolicy:
    """Повторы доставки с экспоненциальным backoff."""

    retries: int
    base_seconds: float
    cap_seconds: float


@dataclass(frozen=True)
class SpooledSnapshot:
    """Снимок, ожидающий отправки."""

    document: JsonObject

    def with_overflow_note(self, dropped: int) -> "SpooledSnapshot":
        """Снимок с записью о выброшенных снимках в ошибках: разрыв истории видно в портале."""
        errors = list(self.document.get("errors") or [])[:ERRORS_KEPT]
        errors.append(f"agent spool overflow: dropped {dropped} oldest unsent snapshot(s)")
        noted = SpooledSnapshot({**self.document, "errors": errors})
        return noted
