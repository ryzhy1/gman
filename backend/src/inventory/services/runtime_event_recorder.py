"""Events Kubernetes как записи истории: каждое новое повторение (uid + счетчик/время) — один раз."""
from inventory.adapters.event_mark_repository import EventMarkRepository
from inventory.entity import EventMark, HistoryEvent, ObservationContext, RuntimeEventReport

RUNTIME_EVENT = "runtime-event"
KUBERNETES_EVENT_SOURCE = "Kubernetes Event"
EVENT_TYPE_FIELD = "type"


class RuntimeEventRecorder:
    """Отбрасывает уже записанные повторы (агент присылает Events снова после relist)."""

    def __init__(self, marks: EventMarkRepository) -> None:
        """Запоминает репозиторий отметок Events."""
        self._marks = marks

    def record(self, context: ObservationContext, reports: list[RuntimeEventReport]) -> list[HistoryEvent]:
        """События истории для новых повторений Events; отметки обновляются."""
        latest_by_uid = {report.uid: report for report in reports}
        events = []
        for report in latest_by_uid.values():
            mark = self._marks.get(context.source_id, report.uid)
            if mark is not None and mark.count >= report.count and mark.last_at >= report.last_at:
                continue
            occurrences = _occurrences(mark, report)
            self._advance(context, mark, report)
            event = _history_event(context, report, occurrences)
            events.append(event)
        return events

    def _advance(self, context: ObservationContext, mark: EventMark | None, report: RuntimeEventReport) -> None:
        """Сдвигает отметку к максимуму счетчика и времени."""
        if mark is None:
            # Узел и причина Event узла питают gman_k8s_node_event_last_timestamp_seconds и карточку узла.
            mark = EventMark(source_id=context.source_id, uid=report.uid, count=0, last_at=report.last_at,
                             received_at=context.received_at, node=_node(report),
                             reason=report.document.get("reason", ""))
        mark.count = max(mark.count, report.count)
        mark.last_at = max(mark.last_at, report.last_at)
        mark.received_at = context.received_at
        self._marks.save(mark)


def _occurrences(mark: EventMark | None, report: RuntimeEventReport) -> int:
    """Сколько повторений добавилось с прошлой записи."""
    if mark is None:
        occurrences = report.count
    elif report.count > mark.count:
        occurrences = report.count - mark.count
    else:
        occurrences = 1
    return occurrences


def _node(report: RuntimeEventReport) -> str:
    """Узел Event узла; у Event pod-а — пусто."""
    node = report.document.get("node", "") if report.is_node_event else ""
    return node


def _history_event(context: ObservationContext, report: RuntimeEventReport, occurrences: int) -> HistoryEvent:
    """Запись истории: поля Event, его тип как event_type и время последнего повторения."""
    fields = {key: value for key, value in report.document.items() if key != EVENT_TYPE_FIELD}
    data = {**context.base_fields(), **fields, "type": RUNTIME_EVENT,
            "event_type": report.document[EVENT_TYPE_FIELD], "occurred_at": report.document["last_at"],
            "occurrences": occurrences, "actor_source": KUBERNETES_EVENT_SOURCE}
    event = HistoryEvent(data=data, received_at=context.received_at, occurred_at=report.last_at)
    return event
