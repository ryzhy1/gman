"""Объекты кластера для снимка: pod-ы, узлы и Events pod-ов и узлов (без них снимок остается полным)."""
import subprocess
from urllib.parse import quote

from collection.entity import JsonObject, KubernetesState
from runtime_access.kubernetes_api import KubernetesApi

PODS_PATH = "/api/v1/pods"
NODES_PATH = "/api/v1/nodes"
# Селектор полей не умеет ИЛИ по двум видам, а все Events принесли бы каждый Normal: два отдельных списка.
EVENTS_PATH = "/api/v1/events?fieldSelector=" + quote("involvedObject.kind=Pod")
NODE_EVENTS_PATH = "/api/v1/events?fieldSelector=" + quote("involvedObject.kind=Node")
EVENT_SOURCES = (("Pod", EVENTS_PATH), ("Node", NODE_EVENTS_PATH))
LIST_ERRORS = (OSError, ValueError, KeyError, subprocess.SubprocessError)


class KubernetesInventory:
    """Списки ресурсов кластера через kubectl или API изнутри кластера."""

    def __init__(self, api: KubernetesApi) -> None:
        """Запоминает доступ к API Kubernetes."""
        self._api = api

    def state(self, pods: list[JsonObject] | None, with_events: bool, errors: list[str]) -> KubernetesState:
        """Pod-ы (если не переданы из кеша watch), узлы и, при with_events, сырые Events."""
        if pods is None:
            pods = self._api.items(["get", "pods", "--all-namespaces"], PODS_PATH)
        node_list = self._api.items(["get", "nodes"], NODES_PATH)
        events = self._events(errors) if with_events else None
        state = KubernetesState(pods=pods, nodes=node_list, events=events)
        return state

    def node_pods(self, node: str) -> list[JsonObject]:
        """Pod-ы одного узла."""
        selector = "spec.nodeName=" + node
        pods = self._api.items(["get", "pods", "--all-namespaces", "--field-selector", selector],
                               PODS_PATH + "?fieldSelector=" + quote(selector))
        return pods

    def _events(self, errors: list[str]) -> list[JsonObject]:
        """Events pod-ов и узлов; нет прав (RBAC без events) — ошибка в снимке, inventory остается полным."""
        events = []
        for kind, path in EVENT_SOURCES:
            try:
                events += self._api.items(["get", "events", "--all-namespaces", "--field-selector",
                                           "involvedObject.kind=" + kind], path)
            except LIST_ERRORS as error:
                # Например, RBAC без events: сам inventory при этом полный.
                errors.append(f"kubernetes {kind.lower()} events unavailable: " + type(error).__name__)
        return events
