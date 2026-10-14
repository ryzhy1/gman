"""Kubernetes: pods as inventory instances, nodes, API access, Events and GPU process attribution."""
import json
import os
import re
import ssl
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import common
from .common import GPU_ENV, int_quantity, newest, origin, parse_time, quantity
from .gpu import POD_UID

# In-cluster API access when kubectl is not installed (e.g. the DaemonSet in deploy/gpu-attribution-k8s.yaml).
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def probe(kind, p):
    if not p:
        return None
    if "httpGet" in p:
        target = f"http :{p['httpGet'].get('port', '')}"
    elif "tcpSocket" in p:
        target = f"tcp :{p['tcpSocket'].get('port', '')}"
    elif "grpc" in p:
        target = f"grpc :{p['grpc'].get('port', '')}"
    else:
        target = "exec"  # The command is not collected: it may embed credentials.
    return f"{kind} {target} period={p.get('periodSeconds', 10)}s failure={p.get('failureThreshold', 3)}"


def env_source(ref):
    """Where a valueFrom variable comes from; Kubernetes resolves it in the kubelet, the Pod spec has no value."""
    for kind, value in ref.items():
        if kind in ("secretKeyRef", "configMapKeyRef"):
            return f"<{kind} {value.get('name', '')}/{value.get('key', '')}>"
        if kind == "fieldRef":
            return f"<fieldRef {value.get('fieldPath', '')}>"
        if kind == "resourceFieldRef":
            return f"<resourceFieldRef {value.get('resource', '')}>"
    return "<valueFrom>"


def pod_env(c):
    """Container env as "NAME=value" in spec order, then envFrom sources as "<envFrom>=<kind name[ prefix]>"."""
    result = [f"{e['name']}={e['value']}" if "value" in e else f"{e['name']}={env_source(e.get('valueFrom') or {})}"
              for e in c.get("env") or [] if e.get("name")]
    for source in c.get("envFrom") or []:
        for kind in ("configMapRef", "secretRef"):
            if kind in source:
                prefix = f" prefix={source['prefix']}" if source.get("prefix") else ""
                result.append(f"<envFrom>=<{kind} {source[kind].get('name', '')}{prefix}>")
    return result


def pod_managers(meta):
    # Field managers name the tool that created/changed the object (kubectl, helm, controllers).
    return ",".join(sorted({m["manager"] for m in meta.get("managedFields") or [] if m.get("manager")}))[:2000]


def pod_instances(pod):
    meta, spec, status = pod["metadata"], pod["spec"], pod.get("status", {})
    labels = meta.get("labels") or {}
    statuses = {s["name"]: s for s in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])}
    owner = next((r for r in meta.get("ownerReferences", []) if r.get("controller")), None)
    pod_security = spec.get("securityContext") or {}
    pod_origin = origin(labels)
    if pod_managers(meta):
        pod_origin["field_managers"] = pod_managers(meta)
    result = []
    for c in spec.get("containers", []) + spec.get("initContainers", []):
        s = statuses.get(c["name"], {})
        state = s.get("state", {})
        current = next(iter(state.values()), {})
        last = (s.get("lastState") or {}).get("terminated") or {}
        resources = c.get("resources") or {}
        limits, requests = resources.get("limits") or {}, resources.get("requests") or {}
        security = c.get("securityContext") or {}
        capabilities = security.get("capabilities") or {}
        env = {e["name"]: e["value"] for e in c.get("env") or [] if e.get("name") in GPU_ENV and "value" in e}
        user = security.get("runAsUser", pod_security.get("runAsUser"))
        result.append({"id": meta["uid"] + ":" + c["name"], "name": meta["name"] + "/" + c["name"],
                       "service_id": labels.get("gman.service", ""),
                       "environment": labels.get("gman.environment", "unknown"),
                       "runtime": "kubernetes", "node": spec.get("nodeName", ""),
                       "namespace": meta.get("namespace", "default"),
                       "controller": f"{owner['kind']}/{owner['name']}" if owner else "",
                       "status": next(iter(state), status.get("phase", "unknown").lower()),
                       "ready": s.get("ready") if s else None,
                       "reason": (current.get("reason") or status.get("reason") or "")[:500],
                       "exit_code": current.get("exitCode"),
                       "last_termination_reason": " ".join(filter(None, (last.get("reason"),
                                                                          f"exit {last['exitCode']}" if "exitCode" in last else ""))),
                       "created_at": meta.get("creationTimestamp"),
                       "started_at": current.get("startedAt"),
                       "finished_at": current.get("finishedAt"),
                       "restarts": s.get("restartCount", 0),
                       "last_oom_at": newest(current.get("finishedAt") if current.get("reason") == "OOMKilled" else None,
                                             last.get("finishedAt") if last.get("reason") == "OOMKilled" else None),
                       "origin": pod_origin,
                       "config": {"image": c["image"], "image_id": s.get("imageID", ""),
                                  "cpu_limit": quantity(limits.get("cpu", "")),
                                  "cpu_request": quantity(requests.get("cpu", "")),
                                  "memory_limit_bytes": int_quantity(limits.get("memory", "")),
                                  "memory_request_bytes": int_quantity(requests.get("memory", "")),
                                  "restart_policy": c.get("restartPolicy") or spec.get("restartPolicy", ""),
                                  "ports": [str(p["containerPort"]) + "/" + p.get("protocol", "TCP") for p in c.get("ports", [])],
                                  "mounts": [m["name"] + " -> " + m["mountPath"] + (" (ro)" if m.get("readOnly") else "")
                                             for m in c.get("volumeMounts", [])],
                                  "network_mode": "host" if spec.get("hostNetwork") else "",
                                  "pid_mode": "host" if spec.get("hostPID") else "",
                                  "ipc_mode": "host" if spec.get("hostIPC") else "",
                                  "user": "" if user is None else str(user),
                                  "env": pod_env(c),
                                  "cap_add": capabilities.get("add") or [],
                                  "cap_drop": capabilities.get("drop") or [],
                                  "probes": [p for p in (probe("liveness", c.get("livenessProbe")),
                                                         probe("readiness", c.get("readinessProbe")),
                                                         probe("startup", c.get("startupProbe"))) if p],
                                  "runtime": spec.get("runtimeClassName") or "",
                                  "gpu_requests": [f"{k}={v}" for k, v in limits.items() if k.endswith("/gpu") or "/mig-" in k]
                                                  + [f"{k}={v}" for k, v in env.items()],
                                  "privileged": security.get("privileged", False),
                                  "readonly_rootfs": security.get("readOnlyRootFilesystem", False)}})
    return result


ACTIVE = ("Pending", "Running", "Unknown")


def nodes(node_list, pods):
    """Node capacity with the sum of requests of the pods placed on it."""
    requested = {}
    for pod in pods:
        name = pod["spec"].get("nodeName")
        if not name or pod.get("status", {}).get("phase") not in ACTIVE:
            continue
        total = requested.setdefault(name, {"cpu": 0.0, "memory": 0, "gpu": 0})
        for c in pod["spec"].get("containers", []):
            resources = c.get("resources") or {}
            req, lim = resources.get("requests") or {}, resources.get("limits") or {}
            total["cpu"] += quantity(req.get("cpu", lim.get("cpu", "0"))) or 0
            total["memory"] += int_quantity(req.get("memory", lim.get("memory", "0"))) or 0
            total["gpu"] += sum(int_quantity(v) or 0 for k, v in lim.items() if k.endswith("/gpu"))
    result = []
    for node in node_list:
        meta, spec, status = node["metadata"], node.get("spec", {}), node.get("status", {})
        labels, info = meta.get("labels") or {}, status.get("nodeInfo") or {}
        capacity, allocatable = status.get("capacity") or {}, status.get("allocatable") or {}
        conditions = {c["type"]: c["status"] for c in status.get("conditions") or []}
        gpus_of = lambda resources: sum(int_quantity(v) or 0 for k, v in resources.items() if k.endswith("/gpu"))
        used = requested.get(meta["name"], {"cpu": 0.0, "memory": 0, "gpu": 0})
        addresses = {a.get("type"): a.get("address", "") for a in status.get("addresses") or []}
        result.append({"name": meta["name"], "ready": conditions.get("Ready") == "True",
                       "internal_ip": addresses.get("InternalIP") or addresses.get("ExternalIP") or "",
                       "unschedulable": bool(spec.get("unschedulable")),
                       "pressure": [k for k, v in conditions.items() if k != "Ready" and v == "True"],
                       "roles": sorted(k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")),
                       "taints": [f"{t['key']}:{t['effect']}" for t in spec.get("taints") or []],
                       "kubelet_version": info.get("kubeletVersion", ""), "os": info.get("osImage", ""),
                       "kernel": info.get("kernelVersion", ""), "runtime_version": info.get("containerRuntimeVersion", ""),
                       "cpu_capacity": quantity(capacity.get("cpu", "")), "cpu_allocatable": quantity(allocatable.get("cpu", "")),
                       "memory_capacity_bytes": int_quantity(capacity.get("memory", "")),
                       "memory_allocatable_bytes": int_quantity(allocatable.get("memory", "")),
                       "gpu_capacity": gpus_of(capacity), "gpu_allocatable": gpus_of(allocatable),
                       "cpu_requested": round(used["cpu"], 3), "memory_requested_bytes": used["memory"],
                       "gpu_requested": used["gpu"],
                       # Set by NVIDIA GPU feature discovery when the GPU operator is installed.
                       "gpu_product": labels.get("nvidia.com/gpu.product", ""),
                       "gpu_memory_mib": int_quantity(labels.get("nvidia.com/gpu.memory", "")),
                       "created_at": meta.get("creationTimestamp")})
    return result


def in_cluster(api_path):
    """Request and TLS context for the in-cluster API with the pod's service account (stdlib only)."""
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        return None
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    host = f"[{host}]" if ":" in host else host
    return Request(f"https://{host}:{port}{api_path}", headers={"Authorization": "Bearer " + token}), context


def kube_get(kubectl_args, api_path):
    """kubectl when installed; otherwise the in-cluster API with the pod's service account."""
    try:
        return json.loads(common.command(["kubectl", *kubectl_args]))
    except FileNotFoundError:
        api = in_cluster(api_path)
        if api is None:
            raise
    with urlopen(api[0], timeout=60, context=api[1]) as response:
        return json.loads(response.read())


def kube_list(kubectl_args, api_path):
    return kube_get([*kubectl_args, "-o", "json"], api_path)


def kube_raw(api_path):
    """A raw API GET; unlike `kubectl get -o json` it keeps the list's resourceVersion for a watch."""
    return kube_get(["get", "--raw", api_path], api_path)


class Gone(Exception):
    """410 Gone: the watch resourceVersion is too old (compacted); only a fresh list recovers."""


def kube_stream(api_path, timeout):
    """Watch events of `api_path` (newline-delimited JSON), through kubectl or the in-cluster API."""
    try:
        process = subprocess.Popen(["kubectl", "get", "--raw", api_path], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        api = in_cluster(api_path)
        if api is None:
            raise
        try:
            response = urlopen(api[0], timeout=timeout + 60, context=api[1])
        except HTTPError as error:
            if error.code == 410:
                raise Gone() from None
            raise
        with response:
            for line in response:
                if line.strip():
                    yield json.loads(line)
        return
    try:
        for line in process.stdout:
            if line.strip():
                yield json.loads(line)
        # kubectl exits non-zero on HTTP errors (a 410 too); the caller relists in either case.
        code = process.wait()
        if code:
            raise subprocess.CalledProcessError(code, "kubectl get --raw")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


# --- Kubernetes Events ----------------------------------------------------------------------------

# Pod Events forwarded to history. Normal noise (Scheduled, Pulling, Pulled, Created, Started) is not.
KUBE_EVENT_REASONS = {"OOMKilling", "BackOff", "FailedScheduling", "Unhealthy", "Killing", "Failed", "Evicted",
                      "FailedMount", "FailedCreatePodSandBox", "Preempting"}
MAX_KUBE_EVENTS = 500
CONTAINER_PATH = re.compile(r"spec\.(?:initContainers|containers|ephemeralContainers)\{([^}]+)\}")
# Event messages are not forwarded: probe output, image references with registry details and command
# errors may carry secrets. Only these fixed phrases are extracted from them.
SCHEDULING_PHRASES = re.compile(
    r"\d+/\d+ nodes are available|\d+ Insufficient [A-Za-z0-9./_-]+|\d+ node\(s\) (?:had untolerated taint"
    r"|didn't match Pod's node affinity/selector|didn't match pod (?:anti-)?affinity rules|were unschedulable"
    r"|had volume node affinity conflict|didn't have free ports for the requested pod ports)")
IMAGE_ERRORS = re.compile(r"\b(ErrImagePull|ImagePullBackOff|ErrImageNeverPull|InvalidImageName|"
                          r"CreateContainerConfigError|CreateContainerError|RunContainerError|PreCreateHookError)\b")


def event_detail(reason, message):
    message = message or ""
    if reason == "Unhealthy":
        probe = re.match(r"(Liveness|Readiness|Startup) probe (failed|errored)", message)
        return f"{probe[1].lower()} probe {probe[2]}" if probe else ""
    if reason == "FailedScheduling":
        return ", ".join(SCHEDULING_PHRASES.findall(message))[:300]
    if reason == "Evicted":
        resource = re.search(r"low on resource: ([a-z-]+)", message)
        return f"node low on {resource[1]}" if resource else ""
    if reason == "BackOff":
        return next((phrase for phrase in ("restarting failed container", "pulling image") if phrase in message), "")
    if reason == "Failed":
        if "Failed to pull image" in message:
            return "image pull failed"
        error = IMAGE_ERRORS.search(message)
        return error[1] if error else ""
    return ""


def pod_ref(pod):
    """What Event mapping needs from a pod: identity, service, node, container names and runtime container IDs."""
    meta, spec, status = pod["metadata"], pod.get("spec") or {}, pod.get("status") or {}
    labels = meta.get("labels") or {}
    return {"uid": meta["uid"], "namespace": meta.get("namespace", "default"), "name": meta["name"],
            "service_id": labels.get("gman.service", ""), "environment": labels.get("gman.environment", "unknown"),
            "node": spec.get("nodeName", ""),
            "containers": {c["name"] for c in spec.get("containers", []) + spec.get("initContainers", [])},
            "container_ids": {runtime_container_id(s["containerID"]): s["name"]
                              for s in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])
                              if s.get("containerID")}}


def kube_event(event, refs):
    """A Kubernetes Event of a pod of a catalogued service, reduced to allowlisted fields; None otherwise.

    `refs` maps pod UID to pod_ref(). The count and the last time let the API dedupe repeats of one Event.
    """
    involved, meta = event.get("involvedObject") or {}, event.get("metadata") or {}
    reason = event.get("reason") or ""
    if involved.get("kind") != "Pod" or reason not in KUBE_EVENT_REASONS or not meta.get("uid"):
        return None
    pod = refs.get(involved.get("uid") or "")
    if pod is None or not pod["service_id"]:
        return None
    path = CONTAINER_PATH.search(involved.get("fieldPath") or "")
    container = path[1] if path and path[1] in pod["containers"] else ""
    series = event.get("series") or {}
    first = event.get("firstTimestamp") or event.get("eventTime")
    last = newest(series.get("lastObservedTime"), event.get("lastTimestamp"), event.get("eventTime"), first)
    if last is None:
        return None
    source = event.get("source") or {}
    return {"uid": meta["uid"][:100], "reason": reason,
            "type": event.get("type") if event.get("type") in ("Normal", "Warning") else "Warning",
            "count": max(1, int(series.get("count") or 0), int(event.get("count") or 0)),
            "first_at": first if parse_time(first) else None, "last_at": last,
            "service_id": pod["service_id"], "environment": pod["environment"],
            "namespace": pod["namespace"], "pod": pod["name"], "container": container,
            "instance_id": pod["uid"] + ":" + container if container else "",
            "instance_name": pod["name"] + ("/" + container if container else ""),
            "component": str(source.get("component") or event.get("reportingComponent") or "")[:100],
            "detail": event_detail(reason, event.get("message"))}


# Node Events (namespace "default"): kubelet, node lifecycle controller and node-problem-detector (NPD).
# kubelet reports memory and PID pressure as NodeHasInsufficientMemory / NodeHasInsufficientPID.
NODE_EVENT_REASONS = {"OOMKilling", "SystemOOM", "NodeNotReady", "NodeHasDiskPressure", "NodeHasInsufficientMemory",
                      "NodeHasInsufficientPID", "EvictionThresholdMet", "Rebooted", "KernelOops", "TaskHung",
                      "FilesystemIsReadOnly", "ImageGCFailed"}
NODE_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
# Only identifiers are looked up in OOM messages, never forwarded: the pod UID of a kubepods cgroup path
# (task_memcg=/kubepods/.../pod<uid>/<container id>) and a 64-hex runtime container ID. The process name and
# pid in the message are free text of the workload and stay on the node.
CONTAINER_HEX = re.compile(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])")


def node_event_detail(reason, message):
    message = message or ""
    if reason in ("OOMKilling", "SystemOOM"):
        if "Memory cgroup out of memory" in message or "CONSTRAINT_MEMCG" in message:
            return "memory cgroup limit"
        if reason == "SystemOOM" or "CONSTRAINT_NONE" in message or re.search(r"(?<!cgroup )[Oo]ut of memory:", message):
            return "node out of memory"
        return ""
    if reason == "EvictionThresholdMet":
        resource = re.search(r"Attempting to reclaim ([a-z][a-z.-]{0,30})", message)
        return f"reclaiming {resource[1]}" if resource else ""
    if reason == "TaskHung":
        blocked = re.search(r"blocked for more than (\d{1,6}) seconds", message)
        return f"task blocked for more than {blocked[1]} seconds" if blocked else ""
    if reason == "KernelOops":
        return next((p for p in ("NULL pointer dereference", "divide error", "general protection fault")
                     if p in message), "")
    return ""


def oom_pod(message, node, refs):
    """(pod_ref, container name) of an OOM node Event whose message names a known pod of this node; else None."""
    message = message or ""
    by_container = {cid: (ref, name) for ref in refs.values() for cid, name in ref.get("container_ids", {}).items()}
    for cid in CONTAINER_HEX.findall(message):
        if cid in by_container:
            ref, container = by_container[cid]
            if ref.get("node") in ("", node):
                return ref, container
    for uid in POD_UID.findall(message):
        ref = refs.get(uid.replace("_", "-"))
        if ref is not None and ref.get("node") in ("", node):
            return ref, ""
    return None


def kube_node_event(event, refs):
    """A Kubernetes Event of a node, reduced to allowlisted fields; None if not allowlisted.

    OOM kills are attributed to a pod (and its service) only when the message names a known pod of the node by
    pod UID or container ID; otherwise to the node alone.
    """
    involved, meta = event.get("involvedObject") or {}, event.get("metadata") or {}
    reason, node = event.get("reason") or "", involved.get("name") or ""
    if (involved.get("kind") != "Node" or reason not in NODE_EVENT_REASONS or not meta.get("uid")
            or not NODE_NAME.fullmatch(node)):
        return None
    series = event.get("series") or {}
    first = event.get("firstTimestamp") or event.get("eventTime")
    last = newest(series.get("lastObservedTime"), event.get("lastTimestamp"), event.get("eventTime"), first)
    if last is None:
        return None
    source = event.get("source") or {}
    mapped = oom_pod(event.get("message"), node, refs) if reason in ("OOMKilling", "SystemOOM") else None
    pod, container = mapped or (None, "")
    return {"uid": meta["uid"][:100], "kind": "Node", "node": node, "reason": reason,
            "type": event.get("type") if event.get("type") in ("Normal", "Warning") else "Warning",
            "count": max(1, int(series.get("count") or 0), int(event.get("count") or 0)),
            "first_at": first if parse_time(first) else None, "last_at": last,
            "service_id": pod["service_id"] if pod else "", "environment": pod["environment"] if pod else "unknown",
            "namespace": pod["namespace"] if pod else "", "pod": pod["name"] if pod else "", "container": container,
            "instance_id": pod["uid"] + ":" + container if container else "",
            "instance_name": (pod["name"] + ("/" + container if container else "")) if pod else "",
            "component": str(source.get("component") or event.get("reportingComponent") or "")[:100],
            "detail": node_event_detail(reason, event.get("message"))}


def kube_events(events, refs):
    """Allowlisted Events of known pods (`refs`: pod UID -> pod_ref) and of nodes, newest first and bounded."""
    mapped = [m for m in ((kube_node_event(e, refs) if (e.get("involvedObject") or {}).get("kind") == "Node"
                           else kube_event(e, refs)) for e in events) if m]
    mapped.sort(key=lambda m: parse_time(m["last_at"]), reverse=True)
    return mapped[:MAX_KUBE_EVENTS]


def node_pods(node):
    return kube_list(["get", "pods", "--all-namespaces", "--field-selector", "spec.nodeName=" + node],
                     "/api/v1/pods?fieldSelector=" + quote("spec.nodeName=" + node))["items"]


def runtime_container_id(value):
    # containerStatuses[].containerID is "<runtime>://<id>": containerd://, cri-o://, docker://.
    return value.split("://", 1)[-1]


def pod_index(pods):
    """Runtime container ID -> pod container, and cgroup pod UID -> pod, from the same pod JSON as the inventory."""
    by_container, by_pod = {}, {}
    for pod in pods:
        meta = pod["metadata"]
        info = {"pod_uid": meta["uid"], "namespace": meta.get("namespace", "default"), "pod": meta["name"]}
        by_pod[meta["uid"]] = info
        # Static pods: the cgroup carries the config hash, while the API shows the mirror pod's own UID.
        config_hash = (meta.get("annotations") or {}).get("kubernetes.io/config.hash")
        if config_hash:
            by_pod[config_hash] = info
        status = pod.get("status") or {}
        # Ephemeral (debug) containers are not inventory instances: they fall back to pod-level attribution.
        for s in status.get("containerStatuses", []) + status.get("initContainerStatuses", []):
            if s.get("containerID"):
                by_container[runtime_container_id(s["containerID"])] = {
                    **info, "container": s["name"], "instance_id": meta["uid"] + ":" + s["name"]}
    return by_container, by_pod


def attribute(gpu_list, pods):
    """Adds pod, container and the inventory instance ID ("<pod uid>:<container>") to GPU processes."""
    by_container, by_pod = pod_index(pods)
    for gpu in gpu_list:
        for process in gpu.get("processes", []):
            match = by_container.get(process.get("container_id")) or by_pod.get(process.get("pod_uid", ""))
            if match:
                process.update(match)


EVENTS_PATH = "/api/v1/events?fieldSelector=" + quote("involvedObject.kind=Pod")
# A field selector cannot OR two kinds, and listing all Events would bring every Normal one: a second list/watch.
NODE_EVENTS_PATH = "/api/v1/events?fieldSelector=" + quote("involvedObject.kind=Node")


def relevant_event(event):
    kind, reason = (event.get("involvedObject") or {}).get("kind"), event.get("reason")
    return reason in KUBE_EVENT_REASONS if kind == "Pod" else kind == "Node" and reason in NODE_EVENT_REASONS
