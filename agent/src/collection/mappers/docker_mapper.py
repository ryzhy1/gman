"""Вывод docker inspect -> документ экземпляра inventory; события контейнера -> экземпляр без inspect."""
import copy
import json

from collection.entity import JsonObject
from collection.mappers.origin_labels import ENVIRONMENT_LABEL, GPU_ENV, SERVICE_LABEL, UNKNOWN_ENVIRONMENT, origin
from runtime_access.docker_cli import ContainerEvent

RUNNING_STATUSES = ("running", "paused", "restarting")
OOM_KILLED = "OOMKilled"
REASON_LIMIT = 500
NEVER_STARTED_PREFIX = "0001-"
NANOSECONDS = 1e9
NO_HEALTHCHECK = "NONE"
# Атрибуты события — это метки контейнера и несколько полей; меткой не считаются.
EVENT_ATTRIBUTES = {"image", "name", "exitCode", "signal", "execDuration"}
DIE = "die"


def docker_instance(container: JsonObject) -> JsonObject:
    """Экземпляр из inspect; окружение сообщается полностью ("NAME=value", значения бывают секретами)."""
    config, host, state = container.get("Config") or {}, container.get("HostConfig") or {}, container.get("State") or {}
    labels = config.get("Labels") or {}
    status = state.get("Status", "unknown")
    running = status in RUNNING_STATUSES
    finished = None if running else _timestamp(state.get("FinishedAt"))
    instance = {"id": container["Id"], "name": container.get("Name", "").lstrip("/"),
                "service_id": labels.get(SERVICE_LABEL, ""),
                "environment": labels.get(ENVIRONMENT_LABEL, UNKNOWN_ENVIRONMENT), "runtime": "docker",
                "controller": "/".join(filter(None, (labels.get("com.docker.compose.project"),
                                                     labels.get("com.docker.compose.service")))),
                "status": status, "health": (state.get("Health") or {}).get("Status", "") if running else "",
                "reason": OOM_KILLED if state.get(OOM_KILLED) else (state.get("Error") or "")[:REASON_LIMIT],
                "exit_code": None if running else state.get("ExitCode"),
                "created_at": _timestamp(container.get("Created")), "started_at": _timestamp(state.get("StartedAt")),
                "finished_at": finished,
                # Docker сбрасывает OOMKilled при перезапуске: --watch добавляет время события `oom`.
                "last_oom_at": finished if state.get(OOM_KILLED) else None,
                "restarts": container.get("RestartCount", 0), "origin": origin(labels),
                "config": _config(container, config, host)}
    return instance


def event_instance(event: ContainerEvent, previous: JsonObject | None) -> JsonObject:
    """Состояние контейнера, который не удалось осмотреть (например, `docker run --rm`), по его событиям."""
    if previous:
        instance = copy.deepcopy(previous)
    else:
        labels = {key: value for key, value in event.attributes.items() if key not in EVENT_ATTRIBUTES}
        known = {"Id": event.container_id, "Name": event.attributes.get("name", ""),
                 "Config": {"Image": event.attributes.get("image", ""), "Labels": labels},
                 "State": {"Status": "running"}}
        instance = docker_instance(known)
        instance["started_at"] = event.time
    if event.action == DIE:
        code = str(event.attributes.get("exitCode", ""))
        instance.update(status="exited", health="", finished_at=event.time,
                        exit_code=int(code) if code.lstrip("-").isdigit() else None)
    return instance


def env_file_location(container: JsonObject) -> tuple[str, str, str]:
    """(ID, StartedAt, рабочий каталог) — где и для какого запуска искать dotenv-файлы."""
    state = container.get("State") or {}
    config = container.get("Config") or {}
    location = (container.get("Id", ""), state.get("StartedAt", ""), config.get("WorkingDir") or "/")
    return location


def attach_env_files(instance: JsonObject, files: dict[str, list[str]]) -> JsonObject:
    """Экземпляр с dotenv-файлами рабочего каталога в конфигурации."""
    instance["config"]["env_files"] = files
    return instance


def _timestamp(value: str | None) -> str | None:
    """Время как есть; Docker сообщает никогда не запускавшиеся контейнеры годом 1."""
    if not value or value.startswith(NEVER_STARTED_PREFIX):
        return None
    return value


def _config(container: JsonObject, config: JsonObject, host: JsonObject) -> JsonObject:
    """Конфигурация запуска контейнера."""
    restart_policy = (host.get("RestartPolicy") or {}).get("Name", "")
    settings = {"image": config.get("Image", ""), "image_id": container.get("Image", ""), "cpu_limit": _cpu_limit(host),
                "memory_limit_bytes": host.get("Memory") or None,
                "memory_request_bytes": host.get("MemoryReservation") or None,
                "cpuset": host.get("CpusetCpus") or "", "restart_policy": restart_policy,
                "ports": _ports(host), "mounts": _mounts(container),
                "networks": list((container.get("NetworkSettings") or {}).get("Networks") or {}),
                "network_mode": host.get("NetworkMode") or "", "pid_mode": host.get("PidMode") or "",
                "ipc_mode": host.get("IpcMode") or "", "user": config.get("User") or "",
                "env": list(config.get("Env") or []),
                # Параметры запуска как заданы (с умолчаниями образа); аргументы бывают секретами.
                "command": list(config.get("Entrypoint") or []), "args": list(config.get("Cmd") or []),
                "cap_add": host.get("CapAdd") or [], "cap_drop": host.get("CapDrop") or [],
                "shm_size_bytes": host.get("ShmSize") or None, "ulimits": _ulimits(host),
                "log_driver": (host.get("LogConfig") or {}).get("Type", ""), "probes": _probes(config),
                "runtime": host.get("Runtime") or "", "gpu_requests": _gpu_requests(config, host),
                "privileged": bool(host.get("Privileged")), "readonly_rootfs": bool(host.get("ReadonlyRootfs"))}
    return settings


def _cpu_limit(host: JsonObject) -> float | None:
    """Лимит CPU в ядрах: NanoCpus или квота/период CFS."""
    quota, period = host.get("CpuQuota", 0), host.get("CpuPeriod", 0)
    limit = host.get("NanoCpus", 0) / NANOSECONDS or (quota / period if quota > 0 and period > 0 else None)
    return limit


def _ports(host: JsonObject) -> list[str]:
    """Публикации портов "адрес:порт->порт/протокол"."""
    ports = []
    for port, bindings in (host.get("PortBindings") or {}).items():
        for binding in bindings or []:
            ports.append(f"{binding.get('HostIp', '')}:{binding.get('HostPort', '')}->{port}")
    return ports


def _mounts(container: JsonObject) -> list[str]:
    """Тома "тип источник -> назначение" с пометкой (ro)."""
    mounts = []
    for mount in container.get("Mounts") or []:
        readonly = "" if mount.get("RW", True) else " (ro)"
        source = mount.get("Name") or mount.get("Source", "")
        mounts.append(f"{mount.get('Type', '')} {source} -> {mount['Destination']}{readonly}")
    return mounts


def _ulimits(host: JsonObject) -> list[str]:
    """Ограничения "имя=мягкое:жесткое"."""
    ulimits = [f"{item['Name']}={item.get('Soft')}:{item.get('Hard')}" for item in host.get("Ulimits") or []]
    return ulimits


def _probes(config: JsonObject) -> list[str]:
    """Healthcheck без самой команды: в ней бывают учетные данные."""
    health = config.get("Healthcheck") or {}
    if not health.get("Test") or health["Test"][0] == NO_HEALTHCHECK:
        return []
    parts = ("healthcheck", f"interval={_duration(health.get('Interval'))}",
             f"timeout={_duration(health.get('Timeout'))}", f"retries={health.get('Retries', 0)}")
    probe = " ".join(filter(None, parts))
    return [probe]


def _duration(nanoseconds: int | None) -> str:
    """Длительность Docker (наносекунды) как "<секунды>s"; не задана — пусто."""
    if not nanoseconds:
        return ""
    text = f"{nanoseconds / NANOSECONDS:g}s"
    return text


def _gpu_requests(config: JsonObject, host: JsonObject) -> list[str]:
    """Запросы устройств GPU и переменные выбора GPU из окружения."""
    requests = []
    for request in host.get("DeviceRequests") or []:
        requests.append(json.dumps({"driver": request.get("Driver"), "ids": request.get("DeviceIDs"),
                                    "count": request.get("Count")}))
    selected = {}
    for entry in config.get("Env") or []:
        name, separator, value = entry.partition("=")
        if separator and name in GPU_ENV:
            selected[name] = value
    requests += [f"{name}={value}" for name, value in selected.items()]
    return requests
