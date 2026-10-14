"""Events Kubernetes pod-ов и узлов -> события истории без свободного текста сообщения.

Сообщения Events не пересылаются: вывод проб, ссылки на образы с деталями registry и ошибки команд могут
содержать секреты. Из них извлекаются только фиксированные фразы.
"""
import re

from collection.entity import POD_UID, JsonObject, PodRef, RuntimeTimestamp

# Events pod-ов, попадающие в историю. Обычный шум (Scheduled, Pulling, Pulled, Created, Started) — нет.
KUBE_EVENT_REASONS = {"OOMKilling", "BackOff", "FailedScheduling", "Unhealthy", "Killing", "Failed", "Evicted",
                      "FailedMount", "FailedCreatePodSandBox", "Preempting"}
# Events узлов (namespace default): kubelet, контроллер жизненного цикла узлов и node-problem-detector (NPD).
NODE_EVENT_REASONS = {"OOMKilling", "SystemOOM", "NodeNotReady", "NodeHasDiskPressure", "NodeHasInsufficientMemory",
                      "NodeHasInsufficientPID", "EvictionThresholdMet", "Rebooted", "KernelOops", "TaskHung",
                      "FilesystemIsReadOnly", "ImageGCFailed"}
OOM_REASONS = ("OOMKilling", "SystemOOM")
MAX_KUBE_EVENTS = 500
UID_LIMIT = 100
COMPONENT_LIMIT = 100
EVENT_TYPES = ("Normal", "Warning")
WARNING = "Warning"
POD = "Pod"
NODE = "Node"
UNKNOWN_ENVIRONMENT = "unknown"
NODE_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
CONTAINER_PATH = re.compile(r"spec\.(?:initContainers|containers|ephemeralContainers)\{([^}]+)\}")
SCHEDULING_PHRASES = re.compile(
    r"\d+/\d+ nodes are available|\d+ Insufficient [A-Za-z0-9./_-]+|\d+ node\(s\) (?:had untolerated taint"
    r"|didn't match Pod's node affinity/selector|didn't match pod (?:anti-)?affinity rules|were unschedulable"
    r"|had volume node affinity conflict|didn't have free ports for the requested pod ports)")
IMAGE_ERRORS = re.compile(r"\b(ErrImagePull|ImagePullBackOff|ErrImageNeverPull|InvalidImageName|"
                          r"CreateContainerConfigError|CreateContainerError|RunContainerError|PreCreateHookError)\b")
DETAIL_LIMIT = 300
PROBE_FAILURE = re.compile(r"(Liveness|Readiness|Startup) probe (failed|errored)")
LOW_RESOURCE = re.compile(r"low on resource: ([a-z-]+)")
BACKOFF_PHRASES = ("restarting failed container", "pulling image")
# В сообщениях OOM ищутся только идентификаторы, они не пересылаются: UID pod-а из пути cgroup kubepods
# и 64-символьный ID контейнера runtime. Имя и PID процесса — свободный текст нагрузки, остаются на узле.
CONTAINER_HEX = re.compile(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])")
NODE_OOM = re.compile(r"(?<!cgroup )[Oo]ut of memory:")
RECLAIMING = re.compile(r"Attempting to reclaim ([a-z][a-z.-]{0,30})")
TASK_BLOCKED = re.compile(r"blocked for more than (\d{1,6}) seconds")
OOPS_PHRASES = ("NULL pointer dereference", "divide error", "general protection fault")


def kube_events(events: list[JsonObject], refs: dict[str, PodRef]) -> list[JsonObject]:
    """Разрешенные Events известных pod-ов (refs: UID pod-а -> ссылка) и узлов, новые первыми, не больше предела."""
    mapped = []
    for event in events:
        is_node = (event.get("involvedObject") or {}).get("kind") == NODE
        document = kube_node_event(event, refs) if is_node else kube_event(event, refs)
        if document:
            mapped.append(document)
    mapped.sort(key=lambda document: RuntimeTimestamp.parse(document["last_at"]), reverse=True)
    bounded = mapped[:MAX_KUBE_EVENTS]
    return bounded


def relevant_event(event: JsonObject) -> bool:
    """Event разрешенной причины своего вида (pod или узел)."""
    kind, reason = (event.get("involvedObject") or {}).get("kind"), event.get("reason")
    if kind == POD:
        relevant = reason in KUBE_EVENT_REASONS
    else:
        relevant = kind == NODE and reason in NODE_EVENT_REASONS
    return relevant


def kube_event(event: JsonObject, refs: dict[str, PodRef]) -> JsonObject | None:
    """Event pod-а сервиса каталога, сведенный к разрешенным полям; иначе None.

    Счетчик и последнее время позволяют API отсекать повторы одного Event.
    """
    involved, meta = event.get("involvedObject") or {}, event.get("metadata") or {}
    reason = event.get("reason") or ""
    if involved.get("kind") != POD or reason not in KUBE_EVENT_REASONS or not meta.get("uid"):
        return None
    pod = refs.get(involved.get("uid") or "")
    times = _times(event)
    if pod is None or not pod.service_id or times is None:
        return None
    path = CONTAINER_PATH.search(involved.get("fieldPath") or "")
    container = path[1] if path and path[1] in pod.containers else ""
    document = {"uid": meta["uid"][:UID_LIMIT], "reason": reason, **_counters(event, times), **_pod_fields(pod, container),
                "component": _component(event), "detail": event_detail(reason, event.get("message"))}
    return document


def kube_node_event(event: JsonObject, refs: dict[str, PodRef]) -> JsonObject | None:
    """Event узла, сведенный к разрешенным полям; иначе None.

    OOM-kill относится к pod-у (и его сервису), только если сообщение называет известный pod этого узла
    по UID pod-а или ID контейнера; иначе — только к узлу.
    """
    involved, meta = event.get("involvedObject") or {}, event.get("metadata") or {}
    reason, node = event.get("reason") or "", involved.get("name") or ""
    allowed = involved.get("kind") == NODE and reason in NODE_EVENT_REASONS and NODE_NAME.fullmatch(node)
    times = _times(event)
    if not allowed or not meta.get("uid") or times is None:
        return None
    mapped = oom_pod(event.get("message"), node, refs) if reason in OOM_REASONS else None
    pod, container = mapped or (None, "")
    owner = _pod_fields(pod, container) if pod else _no_pod_fields()
    document = {"uid": meta["uid"][:UID_LIMIT], "kind": NODE, "node": node, "reason": reason,
                **_counters(event, times), **owner, "component": _component(event),
                "detail": node_event_detail(reason, event.get("message"))}
    return document


def oom_pod(message: str | None, node: str, refs: dict[str, PodRef]) -> tuple[PodRef, str] | None:
    """(pod, контейнер) OOM-Event узла, если сообщение называет известный pod этого узла; иначе None."""
    text = message or ""
    by_container = {}
    for ref in refs.values():
        for container_id, name in ref.container_ids.items():
            by_container[container_id] = (ref, name)
    for container_id in CONTAINER_HEX.findall(text):
        if container_id in by_container and by_container[container_id][0].node in ("", node):
            found = by_container[container_id]
            return found
    for uid in POD_UID.findall(text):
        ref = refs.get(uid.replace("_", "-"))
        if ref is not None and ref.node in ("", node):
            found = (ref, "")
            return found
    return None


def event_detail(reason: str, message: str | None) -> str:
    """Фиксированная фраза из сообщения Event pod-а."""
    extract = POD_DETAILS.get(reason)
    detail = extract(message or "") if extract else ""
    return detail


def node_event_detail(reason: str, message: str | None) -> str:
    """Фиксированная фраза из сообщения Event узла."""
    text = message or ""
    if reason in OOM_REASONS:
        detail = _oom_detail(reason, text)
        return detail
    extract = NODE_DETAILS.get(reason)
    detail = extract(text) if extract else ""
    return detail


def _probe_detail(text: str) -> str:
    """Какая проба не прошла."""
    probe = PROBE_FAILURE.match(text)
    detail = f"{probe[1].lower()} probe {probe[2]}" if probe else ""
    return detail


def _scheduling_detail(text: str) -> str:
    """Почему pod не размещен: только фиксированные фразы планировщика."""
    detail = ", ".join(SCHEDULING_PHRASES.findall(text))[:DETAIL_LIMIT]
    return detail


def _eviction_detail(text: str) -> str:
    """Какого ресурса не хватило узлу."""
    resource = LOW_RESOURCE.search(text)
    detail = f"node low on {resource[1]}" if resource else ""
    return detail


def _backoff_detail(text: str) -> str:
    """Что повторяется с паузой: перезапуск контейнера или загрузка образа."""
    detail = next((phrase for phrase in BACKOFF_PHRASES if phrase in text), "")
    return detail


def _failure_detail(text: str) -> str:
    """Ошибка загрузки образа или запуска контейнера."""
    if "Failed to pull image" in text:
        return "image pull failed"
    error = IMAGE_ERRORS.search(text)
    detail = error[1] if error else ""
    return detail


def _reclaim_detail(text: str) -> str:
    """Какой ресурс узел освобождает вытеснением."""
    resource = RECLAIMING.search(text)
    detail = f"reclaiming {resource[1]}" if resource else ""
    return detail


def _hung_task_detail(text: str) -> str:
    """Сколько задача ядра была заблокирована."""
    blocked = TASK_BLOCKED.search(text)
    detail = f"task blocked for more than {blocked[1]} seconds" if blocked else ""
    return detail


def _oops_detail(text: str) -> str:
    """Вид ошибки ядра."""
    detail = next((phrase for phrase in OOPS_PHRASES if phrase in text), "")
    return detail


def _oom_detail(reason: str, text: str) -> str:
    """Чей лимит памяти исчерпан: cgroup или весь узел."""
    if "Memory cgroup out of memory" in text or "CONSTRAINT_MEMCG" in text:
        return "memory cgroup limit"
    if reason == "SystemOOM" or "CONSTRAINT_NONE" in text or NODE_OOM.search(text):
        return "node out of memory"
    return ""


POD_DETAILS = {"Unhealthy": _probe_detail, "FailedScheduling": _scheduling_detail, "Evicted": _eviction_detail,
               "BackOff": _backoff_detail, "Failed": _failure_detail}
NODE_DETAILS = {"EvictionThresholdMet": _reclaim_detail, "TaskHung": _hung_task_detail, "KernelOops": _oops_detail}


def _times(event: JsonObject) -> tuple[str | None, str] | None:
    """(первое время, последнее время) Event; без последнего времени Event не учитывается."""
    series = event.get("series") or {}
    first = event.get("firstTimestamp") or event.get("eventTime")
    last = RuntimeTimestamp.newest(series.get("lastObservedTime"), event.get("lastTimestamp"),
                                   event.get("eventTime"), first)
    if last is None:
        return None
    times = (first if RuntimeTimestamp.parse(first) else None, last)
    return times


def _counters(event: JsonObject, times: tuple[str | None, str]) -> JsonObject:
    """Тип, счетчик повторений и времена Event."""
    series = event.get("series") or {}
    event_type = event.get("type") if event.get("type") in EVENT_TYPES else WARNING
    count = max(1, int(series.get("count") or 0), int(event.get("count") or 0))
    counters = {"type": event_type, "count": count, "first_at": times[0], "last_at": times[1]}
    return counters


def _pod_fields(pod: PodRef, container: str) -> JsonObject:
    """Сервис, pod, контейнер и экземпляр inventory, к которым относится Event."""
    fields = {"service_id": pod.service_id, "environment": pod.environment, "namespace": pod.namespace,
              "pod": pod.name, "container": container,
              "instance_id": pod.uid + ":" + container if container else "",
              "instance_name": pod.name + ("/" + container if container else "")}
    return fields


def _no_pod_fields() -> JsonObject:
    """Event узла без сопоставленного pod-а."""
    fields = {"service_id": "", "environment": UNKNOWN_ENVIRONMENT, "namespace": "", "pod": "", "container": "",
              "instance_id": "", "instance_name": ""}
    return fields


def _component(event: JsonObject) -> str:
    """Компонент, создавший Event."""
    source = event.get("source") or {}
    component = str(source.get("component") or event.get("reportingComponent") or "")[:COMPONENT_LIMIT]
    return component
