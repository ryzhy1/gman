"""One-shot, read-only inventory. Run periodically with a systemd timer/cron.

Docker: python3 agent/collect.py --runtime docker --source-id server-01
K8s: python3 agent/collect.py --runtime kubernetes --source-id cluster-01
K8s GPU node, run on the node in the host PID namespace (GPU processes attributed to pods):
  python3 agent/collect.py --runtime kubernetes-node --cluster cluster-01 --node gpu-1 --source-id cluster-01.gpu-1
Default emits sanitized JSON; --send submits it to GMAN_URL using GMAN_AGENT_TOKEN
(or the file named by GMAN_AGENT_TOKEN_FILE).
"""
import argparse
import csv
import json
import os
import re
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

AGENT_VERSION = "0.4.0"
# Host /proc. In a container this requires hostPID: nvidia-smi reports PIDs of the caller's PID namespace.
PROC_ROOT = os.environ.get("GMAN_PROC_ROOT", "/proc")
# In-cluster API access when kubectl is not installed (e.g. the DaemonSet in deploy/gpu-attribution-k8s.yaml).
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# Only these labels are forwarded: they identify origin and never carry configuration values.
ORIGIN_LABELS = {
    "com.docker.compose.project": "compose_project",
    "com.docker.compose.service": "compose_service",
    "com.docker.compose.project.working_dir": "compose_working_dir",
    "org.opencontainers.image.source": "image_source",
    "org.opencontainers.image.revision": "image_revision",
    "org.opencontainers.image.version": "image_version",
    "org.opencontainers.image.created": "image_created",
    "app.kubernetes.io/name": "app_name",
    "app.kubernetes.io/version": "app_version",
    "app.kubernetes.io/managed-by": "managed_by",
    "helm.sh/chart": "helm_chart",
}
# Device selection variables are the only env values read: they hold GPU indexes/UUIDs.
GPU_ENV = ("NVIDIA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES")


def command(args):
    # stderr can contain sensitive CLI/context configuration; don't echo it.
    return subprocess.check_output(args, timeout=60, stderr=subprocess.DEVNULL).decode()


def timestamp(value):
    # Docker reports never-started containers as year 1.
    return value if value and not value.startswith("0001-") else None


def origin(labels):
    return {key: str(labels[label])[:2000] for label, key in ORIGIN_LABELS.items() if labels.get(label)}


def duration(nanoseconds):
    return f"{nanoseconds / 1e9:g}s" if nanoseconds else ""


def docker_instance(c):
    cfg, host, state = c.get("Config") or {}, c.get("HostConfig") or {}, c.get("State") or {}
    labels = cfg.get("Labels") or {}
    quota, period = host.get("CpuQuota", 0), host.get("CpuPeriod", 0)
    cpu = host.get("NanoCpus", 0) / 1e9 or (quota / period if quota > 0 and period > 0 else None)
    status = state.get("Status", "unknown")
    running = status in ("running", "paused", "restarting")
    reason = "OOMKilled" if state.get("OOMKilled") else (state.get("Error") or "")[:500]
    health = cfg.get("Healthcheck") or {}
    env = dict(e.split("=", 1) for e in cfg.get("Env") or [] if "=" in e and e.split("=", 1)[0] in GPU_ENV)
    return {"id": c["Id"], "name": c.get("Name", "").lstrip("/"),
            "service_id": labels.get("gman.service", ""),
            "environment": labels.get("gman.environment", "unknown"), "runtime": "docker",
            "controller": "/".join(filter(None, (labels.get("com.docker.compose.project"),
                                                 labels.get("com.docker.compose.service")))),
            "status": status,
            "health": (state.get("Health") or {}).get("Status", "") if running else "",
            "reason": reason,
            "exit_code": None if running else state.get("ExitCode"),
            "created_at": timestamp(c.get("Created")),
            "started_at": timestamp(state.get("StartedAt")),
            "finished_at": None if running else timestamp(state.get("FinishedAt")),
            "restarts": c.get("RestartCount", 0),
            "origin": origin(labels),
            "config": {"image": cfg.get("Image", ""), "image_id": c.get("Image", ""),
                       "cpu_limit": cpu, "memory_limit_bytes": host.get("Memory") or None,
                       "memory_request_bytes": host.get("MemoryReservation") or None,
                       "cpuset": host.get("CpusetCpus") or "",
                       "restart_policy": (host.get("RestartPolicy") or {}).get("Name", ""),
                       "ports": [f"{b.get('HostIp', '')}:{b.get('HostPort', '')}->{p}"
                                 for p, bindings in (host.get("PortBindings") or {}).items() for b in (bindings or [])],
                       "mounts": [f"{m.get('Type', '')} {m.get('Name') or m.get('Source', '')} -> {m['Destination']}"
                                  f"{'' if m.get('RW', True) else ' (ro)'}" for m in c.get("Mounts") or []],
                       "networks": list((c.get("NetworkSettings") or {}).get("Networks") or {}),
                       "network_mode": host.get("NetworkMode") or "",
                       "pid_mode": host.get("PidMode") or "",
                       "ipc_mode": host.get("IpcMode") or "",
                       "user": cfg.get("User") or "",
                       "cap_add": host.get("CapAdd") or [],
                       "cap_drop": host.get("CapDrop") or [],
                       "shm_size_bytes": host.get("ShmSize") or None,
                       "ulimits": [f"{u['Name']}={u.get('Soft')}:{u.get('Hard')}" for u in host.get("Ulimits") or []],
                       "log_driver": (host.get("LogConfig") or {}).get("Type", ""),
                       # The probe command itself is not collected: it may embed credentials.
                       "probes": [" ".join(filter(None, ("healthcheck", f"interval={duration(health.get('Interval'))}",
                                                         f"timeout={duration(health.get('Timeout'))}",
                                                         f"retries={health.get('Retries', 0)}")))]
                                 if health.get("Test") and health["Test"][0] != "NONE" else [],
                       "runtime": host.get("Runtime") or "",
                       "gpu_requests": [json.dumps({"driver": r.get("Driver"), "ids": r.get("DeviceIDs"),
                                                     "count": r.get("Count")}) for r in host.get("DeviceRequests") or []]
                                       + [f"{k}={v}" for k, v in env.items()],
                       "privileged": bool(host.get("Privileged")),
                       "readonly_rootfs": bool(host.get("ReadonlyRootfs"))}}


def inspect_containers():
    ids = command(["docker", "ps", "-aq", "--no-trunc"]).split()
    result, missing = [], []
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        try:
            result.extend(json.loads(command(["docker", "inspect", *batch])))
        except subprocess.CalledProcessError:
            # A container can be removed between `ps` and `inspect`; isolate it.
            for cid in batch:
                try:
                    result.extend(json.loads(command(["docker", "inspect", cid])))
                except subprocess.CalledProcessError:
                    missing.append(cid)
    if missing and set(missing) & set(command(["docker", "ps", "-aq", "--no-trunc"]).split()):
        # Still listed but not inspectable: the daemon is failing, not the container gone.
        raise RuntimeError("docker inspect failed for existing containers")
    return result


def quantity(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)", str(value))
    if not match:
        return None
    scales = {"": 1, "n": 1e-9, "u": 1e-6, "m": .001, "k": 1000,
              "K": 1000, "M": 1e6, "G": 1e9, "T": 1e12,
              "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    scale = scales.get(match[2])
    return float(match[1]) * scale if scale is not None else None


def int_quantity(value):
    number = quantity(value)
    return int(number) if number is not None else None


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


def read_number(path):
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def hwmon(sensor, prefix, label):
    """Read a hwmon channel by its label (edge/junction/mem, sclk/mclk), not by its number."""
    for path in sensor.glob(prefix + "*_label"):
        try:
            if path.read_text().strip() == label:
                return read_number(path.with_name(path.name.replace("_label", "_input")))
        except OSError:
            pass
    return None


def scaled(value, factor):
    return value / factor if value is not None else None


def amd_gpus():
    result = []
    for card in Path("/sys/class/drm").glob("card[0-9]*"):
        if not card.name[4:].isdigit():
            continue
        device = card / "device"
        try:
            if (device / "vendor").read_text().strip() != "0x1002":
                continue
            model = (device / "device").read_text().strip()
        except OSError:
            continue
        sensors = list((device / "hwmon").glob("hwmon*"))
        sensor = sensors[0] if sensors else device / "missing"
        result.append({"id": device.resolve().name, "vendor": "AMD", "model": model, "pci_bus_id": device.resolve().name,
                       "utilization_percent": read_number(device / "gpu_busy_percent"),
                       "memory_utilization_percent": read_number(device / "mem_busy_percent"),
                       "memory_total_bytes": read_number(device / "mem_info_vram_total"),
                       "memory_used_bytes": read_number(device / "mem_info_vram_used"),
                       "temperature_c": scaled(hwmon(sensor, "temp", "edge"), 1000),
                       "hotspot_temperature_c": scaled(hwmon(sensor, "temp", "junction"), 1000),
                       "memory_temperature_c": scaled(hwmon(sensor, "temp", "mem"), 1000),
                       # power1_input on APUs is whole-package (CPU+GPU) power, so only the board average is used.
                       "power_w": scaled(read_number(sensor / "power1_average"), 1e6),
                       "power_limit_w": scaled(read_number(sensor / "power1_cap"), 1e6),
                       "sm_clock_mhz": scaled(hwmon(sensor, "freq", "sclk"), 1e6),
                       "memory_clock_mhz": scaled(hwmon(sensor, "freq", "mclk"), 1e6),
                       "fan_rpm": read_number(sensor / "fan1_input")})
    return result


def nvidia_value(raw, kind):
    raw = raw.strip()
    if not raw or raw.startswith("[") or raw in ("N/A", "Not Supported"):
        return None  # Unsupported on this device/driver: absent, not zero.
    if kind is str:
        return raw
    try:
        return kind(float(raw))
    except ValueError:
        return None


MIB = 1024 ** 2
# (query field, type, output key, multiplier). The base set is supported by every current driver.
NVIDIA_BASE = [("uuid", str, "id", 1), ("name", str, "model", 1), ("pci.bus_id", str, "pci_bus_id", 1),
               ("driver_version", str, "driver_version", 1), ("pstate", str, "pstate", 1),
               ("utilization.gpu", float, "utilization_percent", 1), ("utilization.memory", float, "memory_utilization_percent", 1),
               ("memory.total", float, "memory_total_bytes", MIB), ("memory.used", float, "memory_used_bytes", MIB),
               ("temperature.gpu", float, "temperature_c", 1), ("power.draw", float, "power_w", 1)]
NVIDIA_EXTENDED = [("temperature.memory", float, "memory_temperature_c", 1), ("power.limit", float, "power_limit_w", 1),
                   ("clocks.sm", float, "sm_clock_mhz", 1), ("clocks.mem", float, "memory_clock_mhz", 1),
                   ("clocks.max.sm", float, "max_sm_clock_mhz", 1), ("fan.speed", float, "fan_percent", 1),
                   ("clocks_throttle_reasons.active", str, "throttle_reasons", 1),
                   ("ecc.errors.uncorrected.volatile.total", int, "ecc_uncorrected_errors", 1),
                   ("pcie.link.gen.current", int, "pcie_generation", 1), ("pcie.link.width.current", int, "pcie_width", 1),
                   ("mig.mode.current", str, "mig_mode", 1), ("persistence_mode", str, "persistence_mode", 1)]
THROTTLE_REASONS = {0x1: "gpu_idle", 0x2: "applications_clocks", 0x4: "sw_power_cap", 0x8: "hw_slowdown",
                    0x10: "sync_boost", 0x20: "sw_thermal", 0x40: "hw_thermal", 0x80: "hw_power_brake", 0x100: "display_clocks"}


def throttle_names(mask):
    try:
        value = int(mask, 16)
    except (TypeError, ValueError):
        return []
    return [name for bit, name in THROTTLE_REASONS.items() if value & bit]


# Pod UID in kubepods cgroups: dashes with the cgroupfs driver, underscores in systemd slice names;
# static pods use the 32-hex config hash without separators.
POD_UID = re.compile(r"pod([0-9a-f]{8}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{4}[-_]?[0-9a-f]{12})(?![0-9a-f])")
# A container cgroup is a 64-hex segment, bare (cgroupfs driver) or with a runtime prefix and .scope
# (systemd driver): docker-, cri-containerd-, crio-, libpod-. crio-conmon-<id> is CRI-O's monitor process.
CONTAINER_SEGMENT = re.compile(r"(?:([a-z][a-z0-9-]*)-)?([0-9a-f]{64})(?:\.scope)?")
NO_OWNER = {"container_id": "", "pod_uid": ""}


def parse_cgroup(text):
    """Container ID and pod UID from /proc/<pid>/cgroup (v1 or v2, systemd or cgroupfs); empty for host processes."""
    found = NO_OWNER
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        current = dict(NO_OWNER)
        # ":" splits runc's "slice:prefix:id" form that appears when kubelet and runtime cgroup drivers differ.
        for segment in re.split(r"[/:]", parts[2]):
            match = CONTAINER_SEGMENT.fullmatch(segment)
            if match and "conmon" not in (match[1] or ""):
                current["container_id"] = match[2]  # The innermost container segment wins.
            uid = POD_UID.search(segment)
            if uid:
                current["pod_uid"] = uid[1].replace("_", "-")
        if current["container_id"]:
            return current  # v1 hierarchies agree with each other; the first complete one is enough.
        if current["pod_uid"] and not found["pod_uid"]:
            found = current  # Pod-level cgroup only (e.g. conmon or the pause sandbox).
    return dict(found)


def cgroup_of(pid):
    try:
        return parse_cgroup((Path(PROC_ROOT) / str(pid) / "cgroup").read_text())
    except OSError:
        return dict(NO_OWNER)  # The process exited, or this /proc is not the host's.


def container_of(pid):
    """Docker/containerd/CRI-O container ID from the process cgroup; host processes have none."""
    return cgroup_of(pid)["container_id"]


def nvidia_gpus(errors):
    def query(fields):
        names = ",".join(f[0] for f in fields)
        return list(csv.reader(command(["nvidia-smi", "--query-gpu=" + names, "--format=csv,noheader,nounits"]).splitlines()))
    try:
        fields = NVIDIA_BASE + NVIDIA_EXTENDED
        try:
            rows = query(fields)
        except subprocess.CalledProcessError:
            # Older drivers reject unknown fields as a whole; keep the base set rather than nothing.
            fields = NVIDIA_BASE
            rows = query(fields)
            errors.append("nvidia-smi: extended GPU fields unsupported by this driver")
    except FileNotFoundError:
        return []  # No NVIDIA driver on this host.
    except subprocess.CalledProcessError as error:
        errors.append(f"nvidia-smi failed with exit code {error.returncode}")
        return []
    except subprocess.TimeoutExpired:
        errors.append("nvidia-smi timed out")
        return []
    result = []
    for row in rows:
        gpu = {"vendor": "NVIDIA", "processes": []}
        for (_, kind, key, scale), raw in zip(fields, row):
            value = nvidia_value(raw, kind)
            gpu[key] = value * scale if value is not None and scale != 1 else value
        gpu["throttle_reasons"] = throttle_names(gpu.get("throttle_reasons"))
        result.append(gpu)
    try:
        by_uuid = {g["id"]: g for g in result}
        apps = command(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"])
        for uuid, pid, used in csv.reader(apps.splitlines()):
            gpu = by_uuid.get(uuid.strip())
            if gpu is not None and pid.strip().isdigit():
                memory = nvidia_value(used, float)
                owner = cgroup_of(pid.strip())
                process = {"pid": int(pid), "container_id": owner["container_id"],
                           "used_memory_bytes": int(memory * MIB) if memory is not None else None}
                if owner["pod_uid"]:
                    process["pod_uid"] = owner["pod_uid"]
                gpu["processes"].append(process)
    except (subprocess.SubprocessError, ValueError) as error:
        errors.append("nvidia-smi compute apps unavailable: " + type(error).__name__)
    return result


def gpus(errors):
    return amd_gpus() + nvidia_gpus(errors)


def host_info():
    info = {"kernel": os.uname().release, "cpu_count": os.cpu_count()}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                info["os"] = line.split("=", 1)[1].strip('"')
        info["cpu_model"] = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                                  if line.startswith("model name")), "")
        info["uptime_seconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                info["memory_total_bytes"] = int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        info["runtime_version"] = "docker " + command(["docker", "version", "--format", "{{.Server.Version}}"]).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return info


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
        result.append({"name": meta["name"], "ready": conditions.get("Ready") == "True",
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


def kube_get(kubectl_args, api_path):
    """kubectl when installed; otherwise the in-cluster API with the pod's service account (stdlib only)."""
    try:
        return json.loads(command(["kubectl", *kubectl_args, "-o", "json"]))
    except FileNotFoundError:
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    host = f"[{host}]" if ":" in host else host
    request = Request(f"https://{host}:{port}{api_path}", headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=60, context=context) as response:
        return json.loads(response.read())


def node_pods(node):
    return kube_get(["get", "pods", "--all-namespaces", "--field-selector", "spec.nodeName=" + node],
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


def collect(runtime, source_id, node="", cluster=""):
    observed = datetime.now(timezone.utc).isoformat()
    errors = []
    data = {"source_id": source_id, "observed_at": observed, "agent_version": AGENT_VERSION, "errors": errors}
    if runtime == "kubernetes-node":
        # GPU processes of one node; pods and nodes stay with the cluster source, so this snapshot has no instances.
        gpu_list = nvidia_gpus(errors)
        try:
            attribute(gpu_list, node_pods(node))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            # Processes still carry container ID and pod UID; only names are missing.
            errors.append("pods of the node unavailable: " + type(error).__name__)
        data.update(source_type="kubernetes-node", hostname=node, cluster=cluster, instances=[], gpus=gpu_list,
                    kernel=os.uname().release)
    elif runtime == "docker":
        data.update(source_type="docker-host", hostname=os.uname().nodename,
                    instances=[docker_instance(c) for c in inspect_containers()], gpus=gpus(errors), **host_info())
    else:
        pods = kube_get(["get", "pods", "--all-namespaces"], "/api/v1/pods")["items"]
        node_list = kube_get(["get", "nodes"], "/api/v1/nodes")["items"]
        data.update(source_type="kubernetes-cluster", hostname=source_id,
                    instances=[i for pod in pods for i in pod_instances(pod)], gpus=[], nodes=nodes(node_list, pods))
    return data


def agent_token():
    # A file lets a DaemonSet mount one Secret with a token per node and pick its own by node name.
    path = os.environ.get("GMAN_AGENT_TOKEN_FILE")
    return Path(path).read_text().strip() if path else os.environ["GMAN_AGENT_TOKEN"]


def send(data):
    url = os.environ["GMAN_URL"].rstrip("/")
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1:")):
        raise ValueError("HTTPS required except for local loopback")
    request = Request(url + "/api/inventory", data=json.dumps(data).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + agent_token()})
    try:
        with urlopen(request, timeout=30) as response:
            print(response.status, response.read(500).decode(errors="replace"))
    except HTTPError as error:
        # The API's own error body contains no inventory or credentials.
        raise SystemExit(f"GMan rejected snapshot: HTTP {error.code} {error.read(1000).decode(errors='replace')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=["docker", "kubernetes", "kubernetes-node"], required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--node", default=os.environ.get("NODE_NAME", ""),
                        help="kubernetes-node: this node's name as in Kubernetes (default: NODE_NAME)")
    parser.add_argument("--cluster", default="", help="kubernetes-node: --source-id of the cluster's inventory agent")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    if args.runtime == "kubernetes-node" and not (args.node and args.cluster):
        parser.error("--runtime kubernetes-node needs --node (or NODE_NAME) and --cluster")
    data = collect(args.runtime, args.source_id, args.node, args.cluster)
    if args.send:
        send(data)
    else:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        # Failed collections are never submitted as empty, authoritative snapshots.
        raise SystemExit("Collection/submission failed: " + type(error).__name__)
