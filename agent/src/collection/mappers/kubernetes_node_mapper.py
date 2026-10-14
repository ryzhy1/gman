"""Узлы Kubernetes: емкость, allocatable и сумма запросов pod-ов, размещенных на узле."""
from collection.entity import JsonObject
from collection.mappers.kubernetes_quantity import gpu_count, int_quantity, quantity

# Pod-ы в этих фазах занимают ресурсы узла.
ACTIVE_PHASES = ("Pending", "Running", "Unknown")
READY = "Ready"
TRUE = "True"
ROLE_LABEL_PREFIX = "node-role.kubernetes.io/"
CPU_PRECISION = 3


def nodes(node_list: list[JsonObject], pods: list[JsonObject]) -> list[JsonObject]:
    """Узлы с запросами CPU, памяти и GPU активных pod-ов."""
    requested = _requested(pods)
    result = []
    for node in node_list:
        document = _node(node, requested.get(node["metadata"]["name"], _empty_usage()))
        result.append(document)
    return result


def _empty_usage() -> JsonObject:
    """Нулевые запросы узла."""
    usage = {"cpu": 0.0, "memory": 0, "gpu": 0}
    return usage


def _requested(pods: list[JsonObject]) -> dict[str, JsonObject]:
    """Сумма запросов (иначе лимитов) контейнеров активных pod-ов по узлам."""
    requested: dict[str, JsonObject] = {}
    for pod in pods:
        name = pod["spec"].get("nodeName")
        if not name or pod.get("status", {}).get("phase") not in ACTIVE_PHASES:
            continue
        total = requested.setdefault(name, _empty_usage())
        for container in pod["spec"].get("containers", []):
            _add_container(total, container.get("resources") or {})
    return requested


def _add_container(total: JsonObject, resources: JsonObject) -> None:
    """Добавляет запросы контейнера к сумме узла."""
    requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
    total["cpu"] += quantity(requests.get("cpu", limits.get("cpu", "0"))) or 0
    total["memory"] += int_quantity(requests.get("memory", limits.get("memory", "0"))) or 0
    total["gpu"] += gpu_count(limits)


def _node(node: JsonObject, used: JsonObject) -> JsonObject:
    """Документ узла."""
    meta, spec, status = node["metadata"], node.get("spec", {}), node.get("status", {})
    labels, info = meta.get("labels") or {}, status.get("nodeInfo") or {}
    capacity, allocatable = status.get("capacity") or {}, status.get("allocatable") or {}
    conditions = {condition["type"]: condition["status"] for condition in status.get("conditions") or []}
    addresses = {address.get("type"): address.get("address", "") for address in status.get("addresses") or []}
    document = {"name": meta["name"], "ready": conditions.get(READY) == TRUE,
                "internal_ip": addresses.get("InternalIP") or addresses.get("ExternalIP") or "",
                "unschedulable": bool(spec.get("unschedulable")),
                "pressure": [kind for kind, value in conditions.items() if kind != READY and value == TRUE],
                "roles": sorted(label.split("/", 1)[1] for label in labels if label.startswith(ROLE_LABEL_PREFIX)),
                "taints": [f"{taint['key']}:{taint['effect']}" for taint in spec.get("taints") or []],
                "kubelet_version": info.get("kubeletVersion", ""), "os": info.get("osImage", ""),
                "kernel": info.get("kernelVersion", ""), "runtime_version": info.get("containerRuntimeVersion", ""),
                "cpu_capacity": quantity(capacity.get("cpu", "")), "cpu_allocatable": quantity(allocatable.get("cpu", "")),
                "memory_capacity_bytes": int_quantity(capacity.get("memory", "")),
                "memory_allocatable_bytes": int_quantity(allocatable.get("memory", "")),
                "gpu_capacity": gpu_count(capacity), "gpu_allocatable": gpu_count(allocatable),
                "cpu_requested": round(used["cpu"], CPU_PRECISION), "memory_requested_bytes": used["memory"],
                "gpu_requested": used["gpu"],
                # Ставит NVIDIA GPU feature discovery, когда установлен GPU Operator.
                "gpu_product": labels.get("nvidia.com/gpu.product", ""),
                "gpu_memory_mib": int_quantity(labels.get("nvidia.com/gpu.memory", "")),
                "created_at": meta.get("creationTimestamp")}
    return document
