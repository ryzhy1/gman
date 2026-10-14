"""JSON pod-а Kubernetes -> экземпляры inventory (по контейнеру), ссылка для Events и владельцы процессов GPU."""
from collection.entity import JsonObject, PodContainer, PodIndex, PodRef, RuntimeTimestamp
from collection.mappers.kubernetes_quantity import int_quantity, quantity
from collection.mappers.origin_labels import ENVIRONMENT_LABEL, GPU_ENV, SERVICE_LABEL, UNKNOWN_ENVIRONMENT, origin

DEFAULT_NAMESPACE = "default"
OOM_KILLED = "OOMKilled"
REASON_LIMIT = 500
MANAGERS_LIMIT = 2000
GPU_RESOURCE_SUFFIX = "/gpu"
MIG_RESOURCE_MARKER = "/mig-"
RUNTIME_ID_SEPARATOR = "://"
CONFIG_HASH_ANNOTATION = "kubernetes.io/config.hash"
DEFAULT_PROBE_PERIOD = 10
DEFAULT_PROBE_FAILURES = 3
VALUE_SOURCES = ("secretKeyRef", "configMapKeyRef")
# Ссылки на поля pod-а и ресурсы контейнера: вид -> поле, которое показывается.
VALUE_FIELDS = {"fieldRef": "fieldPath", "resourceFieldRef": "resource"}
ENV_FROM_KINDS = ("configMapRef", "secretRef")


def pod_instances(pod: JsonObject) -> list[JsonObject]:
    """Экземпляр на каждый контейнер и init-контейнер pod-а."""
    meta, spec, status = pod["metadata"], pod["spec"], pod.get("status", {})
    statuses = {item["name"]: item for item in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])}
    pod_origin = origin(meta.get("labels") or {})
    managers = _managers(meta)
    if managers:
        pod_origin["field_managers"] = managers
    instances = []
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        instance = _instance(pod, container, statuses.get(container["name"], {}), pod_origin)
        instances.append(instance)
    return instances


def pod_ref(pod: JsonObject) -> PodRef:
    """Идентичность, сервис, узел, имена контейнеров и ID контейнеров runtime pod-а."""
    meta, spec, status = pod["metadata"], pod.get("spec") or {}, pod.get("status") or {}
    labels = meta.get("labels") or {}
    names = {container["name"] for container in spec.get("containers", []) + spec.get("initContainers", [])}
    container_ids = {}
    for item in status.get("containerStatuses", []) + status.get("initContainerStatuses", []):
        if item.get("containerID"):
            container_ids[runtime_container_id(item["containerID"])] = item["name"]
    ref = PodRef(uid=meta["uid"], namespace=meta.get("namespace", DEFAULT_NAMESPACE), name=meta["name"],
                 service_id=labels.get(SERVICE_LABEL, ""), environment=labels.get(ENVIRONMENT_LABEL, UNKNOWN_ENVIRONMENT),
                 node=spec.get("nodeName", ""), containers=frozenset(names), container_ids=container_ids)
    return ref


def pod_index(pods: list[JsonObject]) -> PodIndex:
    """Владельцы процессов по ID контейнера runtime и по UID pod-а из cgroup (тот же JSON, что у inventory)."""
    by_container, by_pod = {}, {}
    for pod in pods:
        meta = pod["metadata"]
        owner = PodContainer(pod_uid=meta["uid"], namespace=meta.get("namespace", DEFAULT_NAMESPACE), pod=meta["name"])
        by_pod[meta["uid"]] = owner
        # Статические pod-ы: в cgroup хеш конфигурации, а API показывает собственный UID mirror-pod-а.
        config_hash = (meta.get("annotations") or {}).get(CONFIG_HASH_ANNOTATION)
        if config_hash:
            by_pod[config_hash] = owner
        by_container.update(_container_owners(pod, owner))
    index = PodIndex(by_container=by_container, by_pod=by_pod)
    return index


def runtime_container_id(value: str) -> str:
    """ID контейнера из containerStatuses[].containerID ("<runtime>://<id>")."""
    container_id = value.split(RUNTIME_ID_SEPARATOR, 1)[-1]
    return container_id


def _container_owners(pod: JsonObject, owner: PodContainer) -> dict[str, PodContainer]:
    """Контейнеры pod-а по ID runtime; эфемерные (debug) контейнеры — не экземпляры, им остается pod."""
    status = pod.get("status") or {}
    owners = {}
    for item in status.get("containerStatuses", []) + status.get("initContainerStatuses", []):
        if not item.get("containerID"):
            continue
        container = PodContainer(pod_uid=owner.pod_uid, namespace=owner.namespace, pod=owner.pod,
                                 container=item["name"], instance_id=owner.pod_uid + ":" + item["name"])
        owners[runtime_container_id(item["containerID"])] = container
    return owners


def _managers(meta: JsonObject) -> str:
    """Field managers называют инструмент, создавший или изменивший объект (kubectl, helm, контроллеры)."""
    managers = sorted({item["manager"] for item in meta.get("managedFields") or [] if item.get("manager")})
    text = ",".join(managers)[:MANAGERS_LIMIT]
    return text


def _instance(pod: JsonObject, container: JsonObject, status: JsonObject, pod_origin: JsonObject) -> JsonObject:
    """Экземпляр одного контейнера pod-а."""
    meta, spec, pod_status = pod["metadata"], pod["spec"], pod.get("status", {})
    labels = meta.get("labels") or {}
    state = status.get("state", {})
    current = next(iter(state.values()), {})
    last = (status.get("lastState") or {}).get("terminated") or {}
    owner = next((ref for ref in meta.get("ownerReferences", []) if ref.get("controller")), None)
    instance = {"id": meta["uid"] + ":" + container["name"], "name": meta["name"] + "/" + container["name"],
                "service_id": labels.get(SERVICE_LABEL, ""),
                "environment": labels.get(ENVIRONMENT_LABEL, UNKNOWN_ENVIRONMENT), "runtime": "kubernetes",
                "node": spec.get("nodeName", ""), "namespace": meta.get("namespace", DEFAULT_NAMESPACE),
                "controller": f"{owner['kind']}/{owner['name']}" if owner else "",
                "status": next(iter(state), pod_status.get("phase", "unknown").lower()),
                "ready": status.get("ready") if status else None,
                "reason": (current.get("reason") or pod_status.get("reason") or "")[:REASON_LIMIT],
                "exit_code": current.get("exitCode"), "last_termination_reason": _termination(last),
                "created_at": meta.get("creationTimestamp"), "started_at": current.get("startedAt"),
                "finished_at": current.get("finishedAt"), "restarts": status.get("restartCount", 0),
                "last_oom_at": RuntimeTimestamp.newest(_oom_time(current), _oom_time(last)),
                "origin": pod_origin, "config": _config(spec, container, status)}
    return instance


def _termination(last: JsonObject) -> str:
    """Причина и код предыдущего завершения."""
    exit_code = f"exit {last['exitCode']}" if "exitCode" in last else ""
    text = " ".join(filter(None, (last.get("reason"), exit_code)))
    return text


def _oom_time(state: JsonObject) -> str | None:
    """Время завершения, если контейнер убит OOM."""
    moment = state.get("finishedAt") if state.get("reason") == OOM_KILLED else None
    return moment


def _config(spec: JsonObject, container: JsonObject, status: JsonObject) -> JsonObject:
    """Конфигурация запуска контейнера pod-а."""
    resources = container.get("resources") or {}
    limits, requests = resources.get("limits") or {}, resources.get("requests") or {}
    security = container.get("securityContext") or {}
    capabilities = security.get("capabilities") or {}
    user = security.get("runAsUser", (spec.get("securityContext") or {}).get("runAsUser"))
    settings = {"image": container["image"], "image_id": status.get("imageID", ""),
                "cpu_limit": quantity(limits.get("cpu", "")), "cpu_request": quantity(requests.get("cpu", "")),
                "memory_limit_bytes": int_quantity(limits.get("memory", "")),
                "memory_request_bytes": int_quantity(requests.get("memory", "")),
                "restart_policy": container.get("restartPolicy") or spec.get("restartPolicy", ""),
                "ports": [str(port["containerPort"]) + "/" + port.get("protocol", "TCP")
                          for port in container.get("ports", [])],
                "mounts": [mount["name"] + " -> " + mount["mountPath"] + (" (ro)" if mount.get("readOnly") else "")
                           for mount in container.get("volumeMounts", [])],
                "network_mode": "host" if spec.get("hostNetwork") else "",
                "pid_mode": "host" if spec.get("hostPID") else "", "ipc_mode": "host" if spec.get("hostIPC") else "",
                "user": "" if user is None else str(user), "env": _env(container),
                # Только переопределения pod-а: пусто — ENTRYPOINT/CMD образа. Аргументы бывают секретами.
                "command": list(container.get("command") or []), "args": list(container.get("args") or []),
                "cap_add": capabilities.get("add") or [], "cap_drop": capabilities.get("drop") or [],
                "probes": _probes(container), "runtime": spec.get("runtimeClassName") or "",
                "gpu_requests": _gpu_requests(container, limits), "privileged": security.get("privileged", False),
                "readonly_rootfs": security.get("readOnlyRootFilesystem", False)}
    return settings


def _env(container: JsonObject) -> list[str]:
    """Окружение "NAME=value" в порядке spec, затем источники envFrom "<envFrom>=<вид имя[ prefix]>"."""
    result = []
    for entry in container.get("env") or []:
        if not entry.get("name"):
            continue
        value = entry["value"] if "value" in entry else _value_source(entry.get("valueFrom") or {})
        result.append(f"{entry['name']}={value}")
    for source in container.get("envFrom") or []:
        for kind in ENV_FROM_KINDS:
            if kind in source:
                prefix = f" prefix={source['prefix']}" if source.get("prefix") else ""
                result.append(f"<envFrom>=<{kind} {source[kind].get('name', '')}{prefix}>")
    return result


def _value_source(reference: JsonObject) -> str:
    """Откуда берется valueFrom: значение подставляет kubelet, в spec pod-а его нет."""
    for kind, value in reference.items():
        if kind in VALUE_SOURCES:
            source = f"<{kind} {value.get('name', '')}/{value.get('key', '')}>"
            return source
        if kind in VALUE_FIELDS:
            source = f"<{kind} {value.get(VALUE_FIELDS[kind], '')}>"
            return source
    return "<valueFrom>"


def _probes(container: JsonObject) -> list[str]:
    """Пробы контейнера без команд exec: в них бывают учетные данные."""
    probes = []
    for kind in ("liveness", "readiness", "startup"):
        probe = container.get(kind + "Probe")
        if probe:
            probes.append(f"{kind} {_probe_target(probe)} period={probe.get('periodSeconds', DEFAULT_PROBE_PERIOD)}s "
                          f"failure={probe.get('failureThreshold', DEFAULT_PROBE_FAILURES)}")
    return probes


def _probe_target(probe: JsonObject) -> str:
    """Способ проверки и порт."""
    for key, label in (("httpGet", "http"), ("tcpSocket", "tcp"), ("grpc", "grpc")):
        if key in probe:
            target = f"{label} :{probe[key].get('port', '')}"
            return target
    # Команда exec не собирается: в ней бывают учетные данные.
    return "exec"


def _gpu_requests(container: JsonObject, limits: JsonObject) -> list[str]:
    """Лимиты GPU/MIG и переменные выбора GPU из окружения."""
    requests = [f"{name}={value}" for name, value in limits.items()
                if name.endswith(GPU_RESOURCE_SUFFIX) or MIG_RESOURCE_MARKER in name]
    selected = {}
    for entry in container.get("env") or []:
        if entry.get("name") in GPU_ENV and "value" in entry:
            selected[entry["name"]] = entry["value"]
    requests += [f"{name}={value}" for name, value in selected.items()]
    return requests
