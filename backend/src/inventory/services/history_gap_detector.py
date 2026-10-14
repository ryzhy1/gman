"""Разрывы истории: период, изменения за который могут отсутствовать."""
import re
from datetime import datetime

from inventory.entity import HistoryEvent, ObservationContext, ObservedInstance, SnapshotReport

HISTORY_GAP = "history-gap"
SOURCE_SILENT = "source-silent"
SPOOL_OVERFLOW = "spool-overflow"
SPOOL_OVERFLOW_ERROR = re.compile(r"^agent spool overflow: dropped (\d+)")


class HistoryGapDetector:
    """Источник молчал дольше порога или его агент выбросил снимки из spool."""

    def __init__(self, gap_seconds: float) -> None:
        """Запоминает порог молчания источника."""
        self._gap_seconds = gap_seconds

    def detect(self, context: ObservationContext, since: datetime, report: SnapshotReport,
               previous: dict[str, ObservedInstance]) -> list[HistoryEvent]:
        """По событию на каждый затронутый сервис (и одно без сервиса для экземпляров без метки).

        Так разрыв видно и в истории сервиса, и в аудите; портал схлопывает копии в аудите.
        """
        silent = (report.observed_at - since).total_seconds()
        dropped = _dropped_snapshots(report.errors)
        reasons = []
        if silent > self._gap_seconds:
            reasons.append(SOURCE_SILENT)
        if dropped:
            reasons.append(SPOOL_OVERFLOW)
        if not reasons:
            return []
        service_ids = {instance.service_id for instance in [*previous.values(), *report.instances]}
        events = []
        for service_id in sorted(service_ids) or [""]:
            data = {**context.base_fields(), "type": HISTORY_GAP, "service_id": service_id, "reasons": reasons,
                    "gap_from": since.isoformat(), "gap_to": context.observed_at_text, "gap_seconds": round(silent),
                    "dropped_snapshots": dropped}
            event = HistoryEvent(data=data, received_at=context.received_at, occurred_at=context.observed_at)
            events.append(event)
        return events


def _dropped_snapshots(errors: list[str]) -> int:
    """Сколько снимков агент выбросил из переполненного spool (по ошибкам снимка)."""
    dropped = 0
    for error in errors:
        match = SPOOL_OVERFLOW_ERROR.match(error)
        if match:
            dropped += int(match[1])
    return dropped
