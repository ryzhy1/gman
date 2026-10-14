"""События истории из сравнения снимков: подключение источника и изменения экземпляров."""
from inventory.entity import HistoryEvent, ObservationContext, ObservedInstance
from inventory.services.field_changes import FieldChangeDetector

SOURCE_CONNECTED = "source-connected"
INSTANCE_APPEARED = "instance-appeared"
INSTANCE_REMOVED = "instance-removed"


class SnapshotHistoryBuilder:
    """Строит события изменений экземпляров одного снимка."""

    def __init__(self, change_detector: FieldChangeDetector) -> None:
        """Запоминает сравнение документов."""
        self._changes = change_detector

    def connected(self, context: ObservationContext, instance_count: int) -> HistoryEvent:
        """Первый снимок описывает уже работающие экземпляры: это не деплой и не N изменений."""
        data = {**context.base_fields(), "type": SOURCE_CONNECTED, "service_id": "", "instance_count": instance_count}
        event = HistoryEvent(data=data, received_at=context.received_at, occurred_at=context.observed_at)
        return event

    def instance_changes(self, context: ObservationContext, previous: dict[str, ObservedInstance],
                         current: list[ObservedInstance]) -> list[HistoryEvent]:
        """Появления, исчезновения и изменения экземпляров относительно предыдущего снимка."""
        current_by_id = {instance.id: instance for instance in current}
        ordered_ids = list(current_by_id) + [key for key in previous if key not in current_by_id]
        events = []
        for key in ordered_ids:
            before, after = previous.get(key), current_by_id.get(key)
            if before == after:
                continue
            event = self._instance_event(context, key, before, after)
            events.append(event)
        return events

    def _instance_event(self, context: ObservationContext, key: str, before: ObservedInstance | None,
                        after: ObservedInstance | None) -> HistoryEvent:
        """Событие одного изменившегося экземпляра."""
        observed = after or before
        data = {**context.base_fields(), "instance_id": key, "instance_name": observed.name,
                "service_id": observed.service_id}
        if before is None:
            data.update(type=INSTANCE_APPEARED, after=after.document)
        elif after is None:
            data.update(type=INSTANCE_REMOVED, before=before.document)
        else:
            changes = self._changes.changes(before.document, after.document)
            data.update(type=self._changes.classify(changes), changes=changes)
        event = HistoryEvent(data=data, received_at=context.received_at, occurred_at=context.observed_at)
        return event
