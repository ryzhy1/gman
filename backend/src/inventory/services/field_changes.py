"""Изменения по полям между двумя документами и вид изменения экземпляра."""
from typing import Any

from inventory.entity import JsonObject

FIELD_SEPARATOR = "."
# Изменения этих полей — состояние runtime, а не конфигурация.
STATE_FIELDS = {"status", "health", "ready", "reason", "exit_code", "last_termination_reason", "finished_at",
                "last_oom_at"}
RESTART_FIELDS = {"restarts", "started_at"}
LAST_OOM_AT = "last_oom_at"
CONFIG_CHANGED = "config-changed"
INSTANCE_OOM = "instance-oom"
INSTANCE_RESTARTED = "instance-restarted"
STATE_CHANGED = "state-changed"

FieldChanges = dict[str, list[Any]]


class FieldChangeDetector:
    """Сравнивает документы и классифицирует изменения экземпляра."""

    def changes(self, before: JsonObject, after: JsonObject) -> FieldChanges:
        """Изменения по полям; config и origin разворачиваются на один уровень, чтобы история читалась."""
        result: FieldChanges = {}
        for key in before.keys() | after.keys():
            old, new = before.get(key), after.get(key)
            if isinstance(old, dict) and isinstance(new, dict):
                result.update(_nested_changes(key, old, new))
            elif old != new:
                result[key] = [old, new]
        ordered = dict(sorted(result.items()))
        return ordered

    def classify(self, changes: FieldChanges) -> str:
        """Вид изменения: конфигурация, OOM, перезапуск или смена состояния."""
        names = {name.split(FIELD_SEPARATOR)[0] for name in changes}
        if names - STATE_FIELDS - RESTART_FIELDS:
            return CONFIG_CHANGED
        if LAST_OOM_AT in names and changes[LAST_OOM_AT][1]:
            return INSTANCE_OOM
        if names & RESTART_FIELDS:
            return INSTANCE_RESTARTED
        return STATE_CHANGED


def _nested_changes(key: str, old: JsonObject, new: JsonObject) -> FieldChanges:
    """Изменения вложенного объекта с путями "<поле>.<подполе>"."""
    result: FieldChanges = {}
    for sub in old.keys() | new.keys():
        if old.get(sub) != new.get(sub):
            result[key + FIELD_SEPARATOR + sub] = [old.get(sub), new.get(sub)]
    return result
