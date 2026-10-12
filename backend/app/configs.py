"""Structured diff between two stored configurations, for rendering in the portal.

Values are returned as stored, environment variables included (they may hold secrets).
"""
from collections import Counter

MISSING = object()


def entry(path, op, before=None, after=None):
    return {"path": path, "op": op, "before": before, "after": after}


def plain(values):
    return all(v is None or isinstance(v, (str, int, float, bool)) for v in values)


def missing_from(values, other):
    """Elements of `values` not matched by an element of `other`, duplicates counted."""
    left = Counter(map(repr, other))
    result = []
    for value in values:
        if left[repr(value)]:
            left[repr(value)] -= 1
        else:
            result.append(value)
    return result


def diff(before, after, path=""):
    """Changes from `before` to `after` as a flat list ordered by path.

    Nested objects are walked with dotted paths ("origin.app_version"). A key present on one side
    only is "added"/"removed" (older snapshots may lack fields added to the schema later). Lists of
    plain values (ports, mounts, capabilities) are compared as multisets: each element that appeared
    or disappeared is an "added"/"removed" entry with the path "<field>[]"; a pure reordering is one
    "changed" entry with both lists. Anything else that differs is "changed".
    """
    result = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            old, new = before.get(key, MISSING), after.get(key, MISSING)
            sub = f"{path}.{key}" if path else str(key)
            if old is MISSING:
                result.append(entry(sub, "added", after=new))
            elif new is MISSING:
                result.append(entry(sub, "removed", before=old))
            else:
                result += diff(old, new, sub)
    elif isinstance(before, list) and isinstance(after, list) and plain(before) and plain(after):
        if before != after:
            gone, came = missing_from(before, after), missing_from(after, before)
            if not gone and not came:
                result.append(entry(path, "changed", before, after))
            result += [entry(path + "[]", "removed", before=v) for v in gone]
            result += [entry(path + "[]", "added", after=v) for v in came]
    elif before != after:
        result.append(entry(path, "changed", before, after))
    return result


def summary(changes):
    counts = Counter(c["op"] for c in changes)
    return {op: counts.get(op, 0) for op in ("added", "removed", "changed")}
