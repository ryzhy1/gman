"""Сборка модуля watching для Docker (`docker events`) или Kubernetes (watch pod-ов и Events)."""
import time
from collections.abc import Callable

from runtime_access.connection import RuntimeAccess
from watching.adapters.docker_event_feed import DockerEventFeed
from watching.adapters.kubernetes_watch_feed import KubernetesWatchFeed
from watching.entity import EVENTS, NODE_EVENTS, PODS, WatchTimings
from watching.orchestrator import WatchingDependencies, WatchingOrchestrator
from watching.ports import SnapshotSource
from watching.services.docker_watcher import DockerWatcher
from watching.services.event_inbox import EventInbox
from watching.services.kube_watcher import KubeWatcher

PODS_PATH = "/api/v1/pods"
# Селектор полей не умеет ИЛИ по двум видам, а все Events принесли бы каждый Normal: два отдельных потока.
EVENTS_PATH = "/api/v1/events?fieldSelector=involvedObject.kind%3DPod"
NODE_EVENTS_PATH = "/api/v1/events?fieldSelector=involvedObject.kind%3DNode"


def build_docker_watching(source: SnapshotSource, runtime: RuntimeAccess, timings: WatchTimings,
                          clock: Callable[[], float] = time.monotonic) -> WatchingOrchestrator:
    """Режим --watch для Docker: события контейнеров, захват короткоживущих и OOM."""
    watcher = DockerWatcher(clock(), timings, source.capture_container)
    feed = DockerEventFeed(runtime.docker)
    inbox = EventInbox([feed.run], clock)
    dependencies = WatchingDependencies(watcher=watcher, inbox=inbox, source=source)
    orchestrator = WatchingOrchestrator(dependencies)
    return orchestrator


def build_kubernetes_watching(source: SnapshotSource, runtime: RuntimeAccess, timings: WatchTimings,
                              watch_timeout: int, clock: Callable[[], float] = time.monotonic) -> WatchingOrchestrator:
    """Режим --watch для кластера: три потока watch и кеш pod-ов."""
    watcher = KubeWatcher(clock(), timings, source)
    feeds = []
    for resource, path in ((PODS, PODS_PATH), (EVENTS, EVENTS_PATH), (NODE_EVENTS, NODE_EVENTS_PATH)):
        feed = KubernetesWatchFeed(runtime.kubernetes, resource, path, watch_timeout)
        feeds.append(feed.run)
    inbox = EventInbox(feeds, clock)
    dependencies = WatchingDependencies(watcher=watcher, inbox=inbox, source=source)
    orchestrator = WatchingOrchestrator(dependencies)
    return orchestrator
