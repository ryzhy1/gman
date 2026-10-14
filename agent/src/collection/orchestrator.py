"""Оркестратор модуля collection: снимки Docker-хоста, кластера Kubernetes и GPU-узла кластера."""
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from collection.entity import DOCKER, KUBERNETES_NODE, CollectionTarget, JsonObject, PodRef
from collection.mappers.docker_mapper import attach_env_files, docker_instance, env_file_location, event_instance
from collection.mappers.kubernetes_event_mapper import kube_events, relevant_event
from collection.mappers.kubernetes_pod_mapper import pod_index, pod_instances, pod_ref
from collection.mappers.snapshot_mapper import (base_snapshot, cluster_snapshot, docker_snapshot, instances_of,
                                                node_snapshot)
from collection.services.docker_inventory import DockerInventory
from collection.services.docker_oom_history import DockerOomHistory
from collection.services.env_file_reader import EnvFileReader
from collection.services.gpu_inventory import GpuInventory
from collection.services.host_inventory import HostInventory
from collection.services.kubernetes_inventory import KubernetesInventory
from collection.services.pod_attribution import PodAttribution
from runtime_access.docker_cli import ContainerEvent

NODE_POD_ERRORS = (OSError, ValueError, KeyError, subprocess.SubprocessError)


@dataclass(frozen=True)
class CollectionDependencies:
    """Готовые зависимости оркестратора collection."""

    target: CollectionTarget
    docker: DockerInventory
    env_files: EnvFileReader
    gpus: GpuInventory
    host: HostInventory
    kubernetes: KubernetesInventory
    attribution: PodAttribution
    oom_history: DockerOomHistory


class CollectionOrchestrator:
    """Единственная точка входа модуля collection; неудачный сбор — исключение, а не пустой снимок."""

    def __init__(self, dependencies: CollectionDependencies) -> None:
        """Запоминает цель сбора и сервисы модуля."""
        self._deps = dependencies
        self._target = dependencies.target

    def snapshot(self) -> JsonObject:
        """Один снимок для настроенного runtime."""
        if self._target.runtime == KUBERNETES_NODE:
            snapshot = self.node_snapshot()
        elif self._target.runtime == DOCKER:
            snapshot = self.docker_snapshot()
        else:
            snapshot = self.cluster_snapshot()
        return snapshot

    def docker_snapshot(self) -> JsonObject:
        """Снимок Docker-хоста: контейнеры с dotenv-файлами, GPU и сведения о хосте."""
        snapshot = self._docker_snapshot([])
        return snapshot

    def docker_snapshot_with_ooms(self) -> JsonObject:
        """Снимок Docker-хоста по таймеру: плюс OOM-kill с прошлого запуска из событий демона."""
        errors: list[str] = []
        # Собираем снимок.
        snapshot = self._docker_snapshot(errors)
        # Добавляем время OOM из событий `oom` с отметки прошлого запуска.
        instances = instances_of(snapshot)
        self._deps.oom_history.apply(instances, errors, self._target.oom_mark)
        return snapshot

    def _docker_snapshot(self, errors: list[str]) -> JsonObject:
        """Снимок Docker-хоста; ошибки частичного сбора добавляются в `errors`."""
        base = base_snapshot(self._target.source_id, datetime.now(timezone.utc), errors)
        # Осматриваем все контейнеры (удаленные во время сбора пропускаются, отказ демона — ошибка).
        containers = self._deps.docker.inspect_containers()
        # Inspect -> экземпляры inventory с dotenv-файлами рабочих каталогов.
        instances = [self._with_env_files(container) for container in containers]
        # GPU хоста и сведения о хосте.
        gpus = self._deps.gpus.gpus(errors)
        facts = self._deps.host.facts()
        snapshot = docker_snapshot(base, self._deps.host.hostname(), instances, gpus, facts)
        return snapshot

    def cluster_snapshot(self, pods: list[JsonObject] | None = None, with_events: bool = True) -> JsonObject:
        """Снимок кластера; --watch передает pod-ы из своего кеша и добавляет Events сам."""
        errors: list[str] = []
        base = base_snapshot(self._target.source_id, datetime.now(timezone.utc), errors)
        # Читаем pod-ы (если их нет в кеше), узлы и Events.
        state = self._deps.kubernetes.state(pods, with_events, errors)
        # Сопоставляем Events с известными pod-ами.
        events = []
        if state.events is not None:
            events = self.map_events(state.events, self.pod_refs(state.pods))
        snapshot = cluster_snapshot(base, state.pods, state.nodes, events)
        return snapshot

    def node_snapshot(self) -> JsonObject:
        """Снимок GPU-узла: процессы GPU узла, отнесенные к pod-ам."""
        errors: list[str] = []
        base = base_snapshot(self._target.source_id, datetime.now(timezone.utc), errors)
        # GPU NVIDIA узла с владельцами процессов по cgroup.
        gpus = self._deps.gpus.nvidia_gpus(errors)
        # Pod-ы узла -> pod, контейнер и экземпляр каждого процесса; без них остаются ID контейнера и pod-а.
        try:
            pods = self._deps.kubernetes.node_pods(self._target.node)
            index = pod_index(pods)
            self._deps.attribution.attribute(gpus, index)
        except NODE_POD_ERRORS as error:
            errors.append("pods of the node unavailable: " + type(error).__name__)
        snapshot = node_snapshot(base, self._target.node, self._target.cluster, gpus, self._deps.host.kernel())
        return snapshot

    def capture_container(self, event: ContainerEvent, previous: JsonObject | None) -> JsonObject:
        """Состояние контейнера на момент события: inspect, а если контейнер уже удален — по событиям."""
        container = self._deps.docker.inspect_one(event.container_id)
        if container is None:
            instance = event_instance(event, previous)
            return instance
        instance = self._with_env_files(container)
        return instance

    def pod_instances(self, pod: JsonObject) -> list[JsonObject]:
        """Экземпляры pod-а."""
        instances = pod_instances(pod)
        return instances

    def pod_ref(self, pod: JsonObject) -> PodRef:
        """Ссылка на pod для сопоставления Events."""
        ref = pod_ref(pod)
        return ref

    def pod_refs(self, pods: list[JsonObject]) -> dict[str, PodRef]:
        """Ссылки на pod-ы по UID."""
        refs = {}
        for pod in pods:
            ref = pod_ref(pod)
            refs[ref.uid] = ref
        return refs

    def map_events(self, events: list[JsonObject], refs: dict[str, PodRef]) -> list[JsonObject]:
        """Разрешенные Events известных pod-ов и узлов, новые первыми."""
        mapped = kube_events(events, refs)
        return mapped

    def relevant_event(self, event: JsonObject) -> bool:
        """Event разрешенной причины: только такие запускают снимок в --watch."""
        relevant = relevant_event(event)
        return relevant

    def _with_env_files(self, container: JsonObject) -> JsonObject:
        """Экземпляр из inspect с dotenv-файлами рабочего каталога."""
        instance = docker_instance(container)
        container_id, started_at, working_dir = env_file_location(container)
        files = self._deps.env_files.env_files(container_id, started_at, working_dir)
        document = attach_env_files(instance, files)
        return document
