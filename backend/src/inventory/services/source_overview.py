"""Источники для обзора портала: последние снимки с экземплярами и сводкой Events узлов."""
from datetime import datetime, timedelta

from inventory.adapters.event_mark_repository import EventMarkRepository
from inventory.adapters.source_repository import SourceRepository
from inventory.entity import JsonObject, SourceView

KUBERNETES_CLUSTER = "kubernetes-cluster"


class SourceOverview:
    """Документы источников; у кластеров — Events узлов за окно (полный список — в аудите)."""

    def __init__(self, sources: SourceRepository, marks: EventMarkRepository, node_event_window: timedelta) -> None:
        """Запоминает репозитории и окно сводки Events узлов на карточке."""
        self._sources = sources
        self._marks = marks
        self._window = node_event_window

    def sources(self, now: datetime) -> list[SourceView]:
        """Источники с экземплярами; у кластеров — сводка Events узлов за окно."""
        documents = self._sources.documents_with_instances()
        summaries = self._marks.node_summaries(now - self._window)
        views = []
        for document in documents:
            is_cluster = document.get("source_type") == KUBERNETES_CLUSTER
            node_events = summaries.get(document["source_id"], []) if is_cluster else None
            view = SourceView(document=document, node_events=node_events)
            views.append(view)
        return views

    def documents(self) -> list[JsonObject]:
        """Источники с экземплярами без сводки Events (для ссылок на логи)."""
        documents = self._sources.documents_with_instances()
        return documents
