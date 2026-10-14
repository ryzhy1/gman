"""Разбор текстового формата Prometheus для проверок /metrics."""


def samples(text: str) -> dict:
    """{(имя, frozenset(меток)): значение} из текстового формата экспозиции."""
    result = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, value = line.rsplit(" ", 1)
        name, _, rest = head.partition("{")
        labels = frozenset(tuple(pair.split("=", 1)) for pair in rest.rstrip("}").split('",') if pair)
        result[(name, frozenset((k, v.strip('"')) for k, v in labels))] = float(value)
    return result


def find(data: dict, name: str, **labels: str) -> float:
    """Единственное значение метрики с данными метками."""
    found = [v for (n, ls), v in data.items() if n == name and set(labels.items()) <= set(ls)]
    assert len(found) == 1, (name, labels, found)
    return found[0]
