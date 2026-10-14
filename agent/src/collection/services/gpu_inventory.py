"""GPU хоста: NVIDIA через nvidia-smi с владельцами процессов по cgroup, AMD через sysfs."""
import subprocess

from collection.adapters.amd_sysfs import AmdSysfs
from collection.adapters.nvidia_smi import BASE_FIELDS, EXTENDED_FIELDS, NvidiaSmi
from collection.adapters.proc_cgroups import ProcCgroups
from collection.entity import ComputeApp, Gpu, JsonObject, ProcessOwner


class GpuInventory:
    """Показания GPU; ошибки nvidia-smi попадают в ошибки снимка, а не обрывают сбор."""

    def __init__(self, nvidia: NvidiaSmi, amd: AmdSysfs, cgroups: ProcCgroups) -> None:
        """Запоминает адаптеры nvidia-smi, sysfs и cgroup."""
        self._nvidia = nvidia
        self._amd = amd
        self._cgroups = cgroups

    def gpus(self, errors: list[str]) -> list[JsonObject]:
        """GPU AMD и NVIDIA хоста."""
        gpus = self._amd.gpus() + self.nvidia_gpus(errors)
        return gpus

    def nvidia_gpus(self, errors: list[str]) -> list[JsonObject]:
        """GPU NVIDIA с процессами; без драйвера NVIDIA — пустой список."""
        try:
            gpus = self._query_with_fallback(errors)
        except FileNotFoundError:
            return []
        except subprocess.CalledProcessError as error:
            errors.append(f"nvidia-smi failed with exit code {error.returncode}")
            return []
        except subprocess.TimeoutExpired:
            errors.append("nvidia-smi timed out")
            return []
        try:
            self._add_processes(gpus)
        except (subprocess.SubprocessError, ValueError) as error:
            errors.append("nvidia-smi compute apps unavailable: " + type(error).__name__)
        return gpus

    def _query_with_fallback(self, errors: list[str]) -> list[JsonObject]:
        """Расширенный набор полей; старые драйверы отвергают неизвестные поля целиком — тогда базовый."""
        try:
            gpus = self._nvidia.gpus(BASE_FIELDS + EXTENDED_FIELDS)
        except subprocess.CalledProcessError:
            gpus = self._nvidia.gpus(BASE_FIELDS)
            errors.append("nvidia-smi: extended GPU fields unsupported by this driver")
        return gpus

    def _add_processes(self, gpus: list[JsonObject]) -> None:
        """Процессы GPU с контейнером и pod-ом владельца по cgroup."""
        by_uuid = {}
        for document in gpus:
            gpu = Gpu(document)
            by_uuid[gpu.id] = gpu
        for app in self._nvidia.compute_apps():
            gpu = by_uuid.get(app.gpu_uuid)
            if gpu is not None:
                owner = self._cgroups.owner(app.pid)
                gpu.add_process(_process(app, owner))


def _process(app: ComputeApp, owner: ProcessOwner) -> JsonObject:
    """Документ процесса GPU; UID pod-а — только если процесс в pod-е."""
    process = {"pid": app.pid, "container_id": owner.container_id, "used_memory_bytes": app.used_memory_bytes}
    if owner.pod_uid:
        process["pod_uid"] = owner.pod_uid
    return process
