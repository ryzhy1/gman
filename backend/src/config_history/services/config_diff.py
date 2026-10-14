"""Структурное сравнение двух сохраненных конфигураций для показа в портале.

Значения возвращаются как сохранены, включая переменные окружения (в них могут быть секреты).
"""
from collections import Counter
from typing import Any

from config_history.entity import ConfigChange

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"
OPERATIONS = (ADDED, REMOVED, CHANGED)
LIST_ITEM_SUFFIX = "[]"
PATH_SEPARATOR = "."
PLAIN_TYPES = (str, int, float, bool)
_MISSING = object()


class ConfigDiff:
    """Изменения от одной конфигурации к другой плоским списком, упорядоченным по пути.

    Вложенные объекты обходятся с путями через точку ("origin.app_version"). Ключ только с одной стороны —
    added/removed (старые снимки могут не иметь полей, добавленных в схему позже). Списки простых значений
    (порты, тома, capabilities) сравниваются как мультимножества: каждый появившийся или исчезнувший элемент —
    отдельная запись с путем "<поле>[]"; чистая перестановка — одна запись changed с обоими списками.
    """

    def diff(self, before: Any, after: Any, path: str = "") -> list[ConfigChange]:
        """Изменения между двумя значениями по пути `path`."""
        if isinstance(before, dict) and isinstance(after, dict):
            changes = self._diff_objects(before, after, path)
        elif _plain_list(before) and _plain_list(after):
            changes = _diff_lists(before, after, path)
        elif before != after:
            change = ConfigChange(path=path, op=CHANGED, before=before, after=after)
            changes = [change]
        else:
            changes = []
        return changes

    def summary(self, changes: list[ConfigChange]) -> dict[str, int]:
        """Число изменений каждой операции."""
        counts = Counter(change.op for change in changes)
        summary = {op: counts.get(op, 0) for op in OPERATIONS}
        return summary

    def _diff_objects(self, before: dict[str, Any], after: dict[str, Any], path: str) -> list[ConfigChange]:
        """Изменения объекта по ключам в алфавитном порядке."""
        changes = []
        for key in sorted(before.keys() | after.keys()):
            old, new = before.get(key, _MISSING), after.get(key, _MISSING)
            sub_path = f"{path}{PATH_SEPARATOR}{key}" if path else str(key)
            if old is _MISSING:
                change = ConfigChange(path=sub_path, op=ADDED, after=new)
                changes.append(change)
            elif new is _MISSING:
                change = ConfigChange(path=sub_path, op=REMOVED, before=old)
                changes.append(change)
            else:
                changes += self.diff(old, new, sub_path)
        return changes


def _plain_list(value: Any) -> bool:
    """Список из простых значений (или None)."""
    if not isinstance(value, list):
        return False
    plain = all(item is None or isinstance(item, PLAIN_TYPES) for item in value)
    return plain


def _diff_lists(before: list[Any], after: list[Any], path: str) -> list[ConfigChange]:
    """Изменения списка простых значений как мультимножества."""
    if before == after:
        return []
    gone, came = _missing_from(before, after), _missing_from(after, before)
    changes = []
    if not gone and not came:
        reordered = ConfigChange(path=path, op=CHANGED, before=before, after=after)
        changes.append(reordered)
    for value in gone:
        removed = ConfigChange(path=path + LIST_ITEM_SUFFIX, op=REMOVED, before=value)
        changes.append(removed)
    for value in came:
        added = ConfigChange(path=path + LIST_ITEM_SUFFIX, op=ADDED, after=value)
        changes.append(added)
    return changes


def _missing_from(values: list[Any], other: list[Any]) -> list[Any]:
    """Элементы `values`, которым нет пары в `other`, с учетом повторов."""
    left = Counter(repr(item) for item in other)
    result = []
    for value in values:
        if left[repr(value)]:
            left[repr(value)] -= 1
        else:
            result.append(value)
    return result
