"""Ответы Prometheus для подмены HTTP-транспорта в тестах."""
import httpx


def vector(*items: tuple) -> dict:
    """Мгновенный вектор из пар (метки, значение)."""
    return {"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": labels, "value": [0, value]} for labels, value in items]}}


def transport(handler) -> httpx.MockTransport:
    """Транспорт, отвечающий функцией handler(request)."""
    return httpx.MockTransport(handler)
