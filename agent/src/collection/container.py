"""Сборка модуля collection: адаптеры GPU и хоста, сервисы сбора и оркестратор."""
import time
from collections.abc import Callable
from pathlib import Path

from collection.adapters.amd_sysfs import AmdSysfs
from collection.adapters.host_system import HostSystem
from collection.adapters.nvidia_smi import NvidiaSmi
from collection.adapters.oom_mark_store import OomMarkStore
from collection.adapters.proc_cgroups import ProcCgroups
from collection.entity import CollectionTarget
from collection.orchestrator import CollectionDependencies, CollectionOrchestrator
from collection.services.docker_inventory import DockerInventory
from collection.services.docker_oom_history import DockerOomHistory
from collection.services.env_file_reader import EnvFileReader
from collection.services.gpu_inventory import GpuInventory
from collection.services.host_inventory import HostInventory
from collection.services.kubernetes_inventory import KubernetesInventory
from collection.services.pod_attribution import PodAttribution
from config.settings import AgentSettings
from runtime_access.connection import RuntimeAccess

DRM_ROOT = Path("/sys/class/drm")
OOM_MARK_TEMPLATE = "docker-oom-{source_id}.since"


def collection_target(settings: AgentSettings, runtime: str, source_id: str, node: str, cluster: str,
                      state_dir: Path | None = None) -> CollectionTarget:
    """Цель сбора: узел и кластер GPU-узла — из аргументов, иначе из окружения; отметка OOM — в каталоге состояния."""
    directory = state_dir or Path(settings.state_dir)
    mark = directory / OOM_MARK_TEMPLATE.format(source_id=source_id)
    target = CollectionTarget(runtime=runtime, source_id=source_id, node=node or settings.node_name,
                              cluster=cluster or settings.cluster, oom_mark=mark)
    return target


def build_collection_orchestrator(settings: AgentSettings, target: CollectionTarget, runtime: RuntimeAccess,
                                  drm_root: Path = DRM_ROOT,
                                  clock: Callable[[], float] = time.time) -> CollectionOrchestrator:
    """Создает адаптеры, сервисы и оркестратор поверх общего доступа к runtime."""
    proc_root = Path(settings.proc_root)
    cgroups = ProcCgroups(proc_root)
    nvidia = NvidiaSmi(runtime.runner)
    amd = AmdSysfs(drm_root)
    gpus = GpuInventory(nvidia, amd, cgroups)
    host_system = HostSystem(runtime.runner, runtime.docker, settings.host_ip)
    host = HostInventory(host_system)
    marks = OomMarkStore()
    oom_history = DockerOomHistory(runtime.docker, marks, clock)
    docker = DockerInventory(runtime.docker)
    env_files = EnvFileReader(runtime.docker)
    kubernetes = KubernetesInventory(runtime.kubernetes)
    attribution = PodAttribution()
    dependencies = CollectionDependencies(target=target, docker=docker, env_files=env_files, gpus=gpus, host=host,
                                          kubernetes=kubernetes, attribution=attribution, oom_history=oom_history)
    orchestrator = CollectionOrchestrator(dependencies)
    return orchestrator
