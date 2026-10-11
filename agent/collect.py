"""Read-only inventory agent. Run periodically with a systemd timer/cron, or with --watch.

Docker: python3 agent/collect.py --runtime docker --source-id server-01
K8s: python3 agent/collect.py --runtime kubernetes --source-id cluster-01 (pods, nodes, pod and node Events)
K8s GPU node, run on the node in the host PID namespace (GPU processes attributed to pods):
  python3 agent/collect.py --runtime kubernetes-node --cluster cluster-01 --node gpu-1 --source-id cluster-01.gpu-1
Default emits sanitized JSON; --send submits it to GMAN_URL using GMAN_AGENT_TOKEN
(or the file named by GMAN_AGENT_TOKEN_FILE).
With --send, snapshots go through a bounded on-disk spool (oldest first, retried with backoff),
and a non-blocking flock keeps runs from overlapping. --watch follows `docker events` (Docker) or the
Kubernetes watch API for pods and Events (list + watch from resourceVersion, relist on 410 Gone) and sends
a debounced snapshot after changes, so short-lived containers/pods and Docker OOM kills are reported.
Docker without --watch (timer) reads `oom` events since its previous run from `docker events --since/--until`,
so an OOM kill of a container restarted by its restart policy is reported too.
"""
import argparse
import copy
import csv
import errno
import fcntl
import json
import os
import queue
import random
import re
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

AGENT_VERSION = "0.6.0"
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


def parse_time(value):
    """RFC 3339 from Docker/Kubernetes (nanoseconds, "Z") as an aware datetime; None if absent or invalid."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", value or "")
    if not match:
        return None
    try:
        parsed = datetime.fromisoformat(match[1] + "." + (match[2] or "0")[:6].ljust(6, "0")
                                        + ("+00:00" if match[3] == "Z" else match[3]))
    except ValueError:
        return None
    return parsed if parsed.year > 1 else None


def newest(*values):
    """The latest of several timestamps, as given (None when there is none)."""
    known = [(parse_time(v), v) for v in values if parse_time(v)]
    return max(known, key=lambda pair: pair[0])[1] if known else None  # the first of equal times


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
    finished = None if running else timestamp(state.get("FinishedAt"))
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
            "finished_at": finished,
            # Docker clears OOMKilled on restart: --watch adds the time of the `oom` event (Watcher.snapshots).
            "last_oom_at": finished if state.get("OOMKilled") else None,
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
        return json.loads(command(["kubectl", *kubectl_args]))
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


def collect(runtime, source_id, node="", cluster="", pods=None, events=None):
    """One snapshot. Kubernetes --watch passes `pods` from its cache and `events=[]` (it adds Events itself)."""
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
        if pods is None:
            pods = kube_list(["get", "pods", "--all-namespaces"], "/api/v1/pods")["items"]
        node_list = kube_list(["get", "nodes"], "/api/v1/nodes")["items"]
        if events is None:
            raw = []
            for kind, path in (("Pod", EVENTS_PATH), ("Node", NODE_EVENTS_PATH)):
                try:
                    raw += kube_list(["get", "events", "--all-namespaces", "--field-selector",
                                      "involvedObject.kind=" + kind], path)["items"]
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    # E.g. RBAC without events: the inventory itself is still complete.
                    errors.append(f"kubernetes {kind.lower()} events unavailable: " + type(error).__name__)
            events = kube_events(raw, {ref["uid"]: ref for ref in map(pod_ref, pods)})
        data.update(source_type="kubernetes-cluster", hostname=source_id,
                    instances=[i for pod in pods for i in pod_instances(pod)], gpus=[], nodes=nodes(node_list, pods),
                    events=events)
    return data




def log(message):
    print(f"gman-agent: {message}", file=sys.stderr, flush=True)


# --- Delivery -------------------------------------------------------------------------------------

class Retryable(Exception):
    """Transient failure (network, timeout, 5xx, 429): keep the snapshot and retry later."""


class Blocked(Exception):
    """The API refuses this agent (401/403): retrying now is pointless, but the snapshot is kept."""


class Rejected(Exception):
    """The API permanently rejects this snapshot (e.g. 422): drop it so it does not block the spool."""


def agent_token():
    # A file lets a DaemonSet mount one Secret with a token per node and pick its own by node name.
    path = os.environ.get("GMAN_AGENT_TOKEN_FILE")
    return Path(path).read_text().strip() if path else os.environ["GMAN_AGENT_TOKEN"]


def endpoint():
    url = os.environ["GMAN_URL"].rstrip("/")
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1:")):
        raise ValueError("HTTPS required except for local loopback")
    return url + "/api/inventory", agent_token()


def post(data, target):
    url, token = target
    request = Request(url, data=json.dumps(data).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urlopen(request, timeout=30) as response:
            # 200 with accepted=false means a duplicate or older snapshot: replay is idempotent.
            print(response.status, response.read(500).decode(errors="replace"), flush=True)
    except HTTPError as error:
        # The API's own error body contains no inventory or credentials.
        detail = f"HTTP {error.code} {error.read(1000).decode(errors='replace')}"
        if error.code in (408, 425, 429) or error.code >= 500:
            raise Retryable(detail)
        if error.code in (401, 403):
            raise Blocked(detail)
        raise Rejected(detail)
    except (URLError, OSError, HTTPException) as error:
        raise Retryable(type(error).__name__)


def backoff(attempt, base, cap, rng=random.random):
    # Exponential backoff with "equal jitter": at least half of the step, so delays still grow.
    step = min(cap, base * 2 ** attempt)
    return step / 2 + rng() * step / 2


def send_with_retry(data, deliver, retries=3, base=2.0, cap=30.0, sleep=time.sleep, rng=random.random):
    for attempt in range(retries + 1):
        try:
            return deliver(data)
        except Retryable as error:
            if attempt == retries:
                raise
            delay = backoff(attempt, base, cap, rng)
            log(f"delivery failed ({error}); retry {attempt + 1}/{retries} in {delay:.1f}s")
            sleep(delay)


# --- Spool ----------------------------------------------------------------------------------------

class Spool:
    """Unsent snapshots, one JSON file each, named by a monotonic sequence and sent oldest first.

    Bounded by file count and total bytes. The oldest snapshots are dropped first (the server
    ignores snapshots older than the latest accepted one anyway); the drop is reported in the
    errors of the snapshot being added, so the gap is visible in the portal.
    """

    def __init__(self, directory, max_files=100, max_bytes=50 * 1024 ** 2):
        self.directory, self.max_files, self.max_bytes = Path(directory), max(1, max_files), max_bytes
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for stale in self.directory.glob("*.tmp"):
            stale.unlink(missing_ok=True)

    def items(self):
        return sorted(p for p in self.directory.glob("*.json") if p.stem.isdigit())

    def put(self, data):
        files = self.items()
        sequence = max(time.time_ns(), int(files[-1].stem) + 1 if files else 0)
        sizes = [p.stat().st_size for p in files]
        payload = json.dumps(data).encode()
        dropped = 0
        # The newest snapshot is always kept, even if it alone exceeds max_bytes. Once something is
        # dropped, room is reserved for the overflow note appended below.
        while dropped < len(files) and (len(files) - dropped + 1 > self.max_files or sum(sizes[dropped:])
                                        + len(payload) + (128 if dropped else 0) > self.max_bytes):
            files[dropped].unlink(missing_ok=True)
            dropped += 1
        if dropped:
            log(f"spool full: dropped {dropped} oldest unsent snapshot(s)")
            data = dict(data, errors=list(data.get("errors") or [])[:49]
                        + [f"agent spool overflow: dropped {dropped} oldest unsent snapshot(s)"])
            payload = json.dumps(data).encode()
        path = self.directory / f"{sequence:020d}.json"
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path

    def flush(self, deliver):
        """Send spooled snapshots in order, stopping at the first that cannot be delivered now.

        Returns the number of snapshots still waiting.
        """
        items = self.items()
        for index, path in enumerate(items):
            try:
                data = json.loads(path.read_bytes())
            except (OSError, ValueError):
                log(f"dropping unreadable spool file {path.name}")
                path.unlink(missing_ok=True)
                continue
            try:
                deliver(data)
            except Rejected as error:
                log(f"snapshot {path.name} rejected, dropped: {error}")
            except (Retryable, Blocked) as error:
                log(f"delivery failed, {len(items) - index} snapshot(s) kept in spool: {error}")
                return len(items) - index
            path.unlink(missing_ok=True)
        return 0


# --- Run lock -------------------------------------------------------------------------------------

def acquire_lock(path):
    """Non-blocking exclusive flock. Returns the open fd (held until exit), or None if busy."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd


# --- Watch mode -----------------------------------------------------------------------------------

# Container events that change what a snapshot shows. exec_* (healthchecks), attach, resize, top
# and similar are ignored: they would trigger a snapshot every healthcheck interval.
WATCH_TRIGGERS = {"create", "start", "restart", "die", "oom", "kill", "stop", "pause", "unpause",
                  "update", "rename", "destroy", "health_status"}
# On these the container is inspected immediately: it may be gone by the next snapshot.
WATCH_CAPTURE = {"start", "die"}
EVENT_ATTRIBUTES = {"image", "name", "exitCode", "signal", "execDuration"}


def event_id(event):
    return (event.get("Actor") or {}).get("ID") or event.get("id")


def event_time(event):
    nanoseconds = event.get("timeNano") or int(event.get("time") or 0) * 10 ** 9
    return datetime.fromtimestamp(nanoseconds / 1e9, timezone.utc).isoformat() if nanoseconds else None


def event_instance(event, previous):
    """Best-effort state of a container that could not be inspected (e.g. `docker run --rm`)."""
    attributes = (event.get("Actor") or {}).get("Attributes") or {}
    when = event_time(event)
    if previous:
        instance = copy.deepcopy(previous)
    else:
        # Event attributes are the container labels plus a few fields; only allowlisted labels are used.
        labels = {k: v for k, v in attributes.items() if k not in EVENT_ATTRIBUTES}
        instance = docker_instance({"Id": event_id(event), "Name": attributes.get("name", ""),
                                    "Config": {"Image": attributes.get("image", ""), "Labels": labels},
                                    "State": {"Status": "running"}})
        instance["started_at"] = when
    if event.get("Action") == "die":
        code = str(attributes.get("exitCode", ""))
        instance.update(status="exited", health="", finished_at=when,
                        exit_code=int(code) if code.lstrip("-").isdigit() else None)
    return instance


def capture(event, previous):
    try:
        return docker_instance(json.loads(command(["docker", "inspect", event_id(event)]))[0])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError, KeyError):
        return event_instance(event, previous)


class Watcher:
    """Debounce for `docker events`.

    A snapshot is due after `debounce` s without events, at most `max_delay` s after the first
    pending event, and at least every `interval` s. Containers captured on start/die are kept
    until the next snapshot so that short-lived ones can still be reported.
    """

    def __init__(self, now, debounce=2.0, max_delay=10.0, interval=60.0, capture=capture):
        self.debounce, self.max_delay, self.interval, self.capture = debounce, max_delay, interval, capture
        self.first = self.last = None
        self.next_poll = now  # full snapshot right after start
        self.seen = {}
        # Container ID -> time of the latest `oom` event. Docker clears State.OOMKilled when a restart
        # policy restarts the container, and an OOM kill of a child process does not stop it at all.
        self.ooms = {}

    def handle(self, event, now):
        if event.get("Type", "container") != "container":
            return
        action = (event.get("Action") or event.get("status") or "").split(":")[0].strip()
        if action not in WATCH_TRIGGERS:
            return
        cid = event_id(event)
        if action == "oom" and cid:
            self.ooms[cid] = newest(self.ooms.get(cid), event_time(event) or datetime.now(timezone.utc).isoformat())
        if action in WATCH_CAPTURE and cid:
            self.seen[cid] = self.capture(event, self.seen.get(cid))
        self.trigger(now)

    def trigger(self, now):
        self.last = now
        if self.first is None:
            self.first = now

    def snapshots(self, data, seen):
        """Snapshots to send for one collection: ephemeral containers and OOM times added."""
        result = with_ephemeral(data, seen)
        if self.ooms:
            for instance in {id(i): i for snapshot in result for i in snapshot["instances"]}.values():
                if instance["id"] in self.ooms:
                    instance["last_oom_at"] = newest(instance.get("last_oom_at"), self.ooms[instance["id"]])
            # Reported while the container exists; the API keeps the latest time once it is recorded.
            self.ooms = {cid: at for cid, at in self.ooms.items() if cid in {i["id"] for i in data["instances"]}}
        return result

    def resync(self, now):
        self.next_poll = min(self.next_poll, now)

    def deadline(self):
        if self.last is None:
            return self.next_poll
        return min(self.next_poll, self.last + self.debounce, self.first + self.max_delay)

    def due(self, now):
        return now >= self.deadline()

    def take(self, now):
        seen, self.seen = self.seen, {}
        self.first = self.last = None
        self.next_poll = now + self.interval
        return seen

    def retry(self, seen, now, delay):
        self.seen = {**seen, **self.seen}
        self.next_poll = min(self.next_poll, now + delay)


def with_ephemeral(data, seen):
    """Snapshots to send for one collection.

    Containers seen in events but absent now (removed, e.g. --rm) go into an interim snapshot with
    their last captured state, followed 1 ms later by the real one, so the API records both their
    appearance/exit and their removal.
    """
    present = {i["id"] for i in data["instances"]}
    gone = [instance for cid, instance in seen.items() if cid not in present]
    if not gone:
        return [data]
    interim = dict(data, instances=data["instances"] + gone)
    observed = datetime.fromisoformat(data["observed_at"])
    return [interim, dict(data, observed_at=(observed + timedelta(milliseconds=1)).isoformat())]


def docker_events(lines, reconnect=5.0, popen=subprocess.Popen, sleep=time.sleep, restarts=None):
    """Feed `docker events` lines into a queue; None marks a broken stream (resync needed)."""
    while restarts is None or restarts > 0:
        try:
            process = popen(["docker", "events", "--format", "{{json .}}", "--filter", "type=container"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in process.stdout:
                lines.put(line)
            process.wait()
        except OSError as error:
            log(f"docker events unavailable: {type(error).__name__}")
        lines.put(None)
        if restarts is not None:
            restarts -= 1
        sleep(reconnect)


def watch_path(api_path, version, timeout):
    separator = "&" if "?" in api_path else "?"
    return (f"{api_path}{separator}watch=1&allowWatchBookmarks=true&resourceVersion={quote(str(version))}"
            f"&timeoutSeconds={int(timeout)}")


def kube_watch(resource, api_path, items, list_=kube_raw, stream=kube_stream, sleep=time.sleep, timeout=300,
               rng=random.random, attempts=None):
    """List `api_path`, then watch it from the list's resourceVersion, feeding dicts into `items`.

    {"resource", "type": "SYNC", "items"} after every (re)list, {"resource", "type", "object"} for
    ADDED/MODIFIED/DELETED. BOOKMARK only advances the resourceVersion. A watch the server ends after
    `timeout` resumes from the last resourceVersion; 410 Gone (expired version) relists at once; other
    failures put None (resync) and relist after an exponential backoff. `attempts` bounds the loop for tests.
    """
    version, failures = None, 0
    while attempts is None or attempts > 0:
        if attempts is not None:
            attempts -= 1
        try:
            if version is None:
                listing = list_(api_path)
                version = (listing.get("metadata") or {})["resourceVersion"]
                items.put({"resource": resource, "type": "SYNC", "items": listing.get("items") or []})
            for event in stream(watch_path(api_path, version, timeout), timeout):
                kind, obj = event.get("type"), event.get("object") or {}
                if kind == "ERROR":
                    # The API reports an expired resourceVersion as a Status object inside the stream.
                    if obj.get("code") == 410:
                        raise Gone()
                    raise RuntimeError(f"watch error {obj.get('code')} {obj.get('reason', '')}")
                version = (obj.get("metadata") or {}).get("resourceVersion") or version
                failures = 0
                if kind in ("ADDED", "MODIFIED", "DELETED"):
                    items.put({"resource": resource, "type": kind, "object": obj})
        except Gone:
            log(f"{resource} watch: resourceVersion expired (410 Gone); relisting")
            version = None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            delay = backoff(failures, 1.0, 60.0, rng)
            failures += 1
            log(f"{resource} watch failed: {type(error).__name__}; relist in {delay:.1f}s")
            version = None
            items.put(None)
            sleep(delay)


class KubeWatcher(Watcher):
    """Debounce for the Kubernetes watch of pods, pod Events and node Events, with a pod cache built from it.

    Snapshots are built from the cache (nodes are still listed per snapshot). Pods seen in the window are
    kept with their last state, so a pod created and deleted between two snapshots is still reported
    (with_ephemeral). Deleted pods stay known for `keep_deleted` seconds to map their late Events.
    """

    def __init__(self, now, debounce=2.0, max_delay=10.0, interval=60.0, keep_deleted=600.0):
        super().__init__(now, debounce, max_delay, interval, capture=None)
        self.pods, self.deleted, self.events = {}, {}, {}
        self.synced, self.keep_deleted, self.now = False, keep_deleted, now

    def handle(self, item, now):
        resource, kind = item.get("resource"), item.get("type")
        self.now = now
        if resource == "pods":
            if kind == "SYNC":
                fresh = {p["metadata"]["uid"]: p for p in item.get("items") or []}
                # Pods deleted while the watch was down: their Events can still arrive.
                self.deleted.update({uid: (pod_ref(pod), now) for uid, pod in self.pods.items() if uid not in fresh})
                self.pods, self.synced = fresh, True
            elif kind in ("ADDED", "MODIFIED", "DELETED"):
                pod = item["object"]
                uid = pod["metadata"]["uid"]
                if kind == "DELETED":
                    self.pods.pop(uid, None)
                    self.deleted[uid] = (pod_ref(pod), now)
                else:
                    self.pods[uid] = pod
                for instance in pod_instances(pod):
                    self.seen[instance["id"]] = instance
            else:
                return
        elif resource in ("events", "node-events") and kind in ("SYNC", "ADDED", "MODIFIED"):
            fresh = (item.get("items") or []) if kind == "SYNC" else [item["object"]]
            relevant = [e for e in fresh if relevant_event(e)]
            for event in relevant:
                self.events[(event.get("metadata") or {}).get("uid")] = event
            if not relevant:
                return
        else:
            return
        self.trigger(now)

    def pod_list(self):
        """Cached pods, or None before the first list (the snapshot then lists them itself)."""
        return list(self.pods.values()) if self.synced else None

    def snapshots(self, data, seen):
        self.deleted = {uid: v for uid, v in self.deleted.items() if self.now - v[1] <= self.keep_deleted}
        refs = {uid: ref for uid, (ref, _) in self.deleted.items()}
        refs.update({uid: pod_ref(pod) for uid, pod in self.pods.items()})
        # Events not mapped to a known pod now are dropped: a relist brings back those still stored.
        events = kube_events(self.events.values(), refs)
        self.events = {}
        result = [dict(snapshot, events=[]) for snapshot in with_ephemeral(data, seen)]
        result[-1]["events"] = events
        return result


def watch(collect_snapshot, spool, deliver, watcher, lines=None, clock=time.monotonic,
          retry_delay=10.0, cycles=None, feeds=(docker_events,)):
    """Debounced snapshots driven by a stream. Queue items: a JSON line (docker events), a dict (Kubernetes
    watch, see kube_watch) or None for a broken stream (resync)."""
    if lines is None:
        lines = queue.Queue()
        for feed in feeds:
            threading.Thread(target=feed, args=(lines,), daemon=True).start()
    done = 0
    while cycles is None or done < cycles:
        try:
            line = lines.get(timeout=max(0.0, watcher.deadline() - clock()))
        except queue.Empty:
            line = ""
        now = clock()
        if line is None:
            watcher.resync(now)  # events may have been missed while the stream was down
        elif isinstance(line, dict):
            watcher.handle(line, now)
        elif line.strip():
            try:
                watcher.handle(json.loads(line), now)
            except ValueError:
                log("ignoring malformed docker event")
        if not watcher.due(now):
            continue
        done += 1
        seen = watcher.take(now)
        try:
            data = collect_snapshot()
        except Exception as error:
            # As in one-shot mode, a failed collection is never sent as an empty snapshot.
            log(f"collection failed: {type(error).__name__}; retry in {retry_delay:g}s")
            watcher.retry(seen, now, retry_delay)
            continue
        for snapshot in watcher.snapshots(data, seen):
            spool.put(snapshot)
        spool.flush(deliver)


# --- Docker OOM without --watch -----------------------------------------------------------------------

# The daemon keeps only its latest events in memory (a few hundred): a longer look-back finds nothing more.
OOM_LOOKBACK = 3600


def docker_ooms(since, until, run=command):
    """Container ID -> time of the latest `oom` event between two Unix times (`docker events` returns at --until)."""
    output = run(["docker", "events", "--since", str(since), "--until", str(until), "--filter", "type=container",
                  "--filter", "event=oom", "--format", "{{json .}}"])
    result = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        cid, when = event_id(event), event_time(event)
        if cid and when:
            result[cid] = newest(result.get(cid), when)
    return result


def with_docker_ooms(data, mark, clock=time.time, run=command):
    """One-shot Docker: OOM kills since the previous run (time kept in `mark`) as last_oom_at.

    Docker clears State.OOMKilled when the restart policy restarts the container, and an OOM kill of a child
    process does not stop it at all; the `oom` event stays in the daemon's event buffer. The API keeps the
    latest last_oom_at once recorded. Removed containers (--rm) have no instance to carry it.
    """
    until = int(clock())
    try:
        since = max(int(Path(mark).read_text().strip()), until - OOM_LOOKBACK)
    except (OSError, ValueError):
        since = until - OOM_LOOKBACK
    if since >= until:
        return data
    try:
        ooms = docker_ooms(since, until, run)
    except (OSError, subprocess.SubprocessError) as error:
        data["errors"].append("docker oom events unavailable: " + type(error).__name__)
        return data  # the mark stays: the next run looks at this period again
    for instance in data["instances"]:
        if instance["id"] in ooms:
            instance["last_oom_at"] = newest(instance.get("last_oom_at"), ooms[instance["id"]])
    temporary = Path(str(mark) + ".tmp")
    temporary.write_text(str(until))
    os.replace(temporary, mark)
    return data


# --- CLI ------------------------------------------------------------------------------------------

def default_state_dir():
    if os.environ.get("GMAN_AGENT_STATE_DIR"):
        return os.environ["GMAN_AGENT_STATE_DIR"]
    return "/var/lib/gman-agent" if os.geteuid() == 0 else str(Path.home() / ".local/state/gman-agent")


def run_once(collect_snapshot, spool, deliver):
    try:
        data = collect_snapshot()
    except Exception:
        # A failed collection is never submitted as an empty, authoritative snapshot;
        # snapshots spooled earlier are still delivered.
        spool.flush(deliver)
        raise
    spool.put(data)
    remaining = spool.flush(deliver)
    if remaining:
        raise SystemExit(f"Submission failed: {remaining} snapshot(s) kept in spool for retry")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", choices=["docker", "kubernetes", "kubernetes-node"], required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--node", default=os.environ.get("NODE_NAME", ""),
                        help="kubernetes-node: this node's name as in Kubernetes (default: NODE_NAME)")
    # Environment defaults let the systemd unit (deploy/systemd, ExecStart without these flags) run this mode.
    parser.add_argument("--cluster", default=os.environ.get("GMAN_CLUSTER", ""),
                        help="kubernetes-node: --source-id of the cluster's inventory agent (default: GMAN_CLUSTER)")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--watch", action="store_true",
                        help="follow docker events or the Kubernetes watch API (requires --send; docker, kubernetes)")
    parser.add_argument("--state-dir", help="spool and lock directory (default: GMAN_AGENT_STATE_DIR, "
                                            "/var/lib/gman-agent as root, else ~/.local/state/gman-agent)")
    parser.add_argument("--spool-max-files", type=int, default=100)
    parser.add_argument("--spool-max-bytes", type=int, default=50 * 1024 ** 2)
    parser.add_argument("--retries", type=int, default=3, help="delivery retries with exponential backoff")
    parser.add_argument("--debounce", type=float, default=2.0, help="--watch: seconds of quiet before a snapshot")
    parser.add_argument("--max-delay", type=float, default=10.0, help="--watch: max seconds an event waits")
    parser.add_argument("--interval", type=float, default=60.0, help="--watch: full snapshot at least this often")
    parser.add_argument("--watch-timeout", type=int, default=300,
                        help="--watch kubernetes: seconds before the API server ends a watch (it is resumed)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.source_id) or not args.source_id.strip("."):
        parser.error("invalid --source-id")
    if args.runtime == "kubernetes-node" and not (args.node and args.cluster):
        parser.error("--runtime kubernetes-node needs --node (or NODE_NAME) and --cluster")
    if args.watch and (args.runtime not in ("docker", "kubernetes") or not args.send):
        parser.error("--watch requires --runtime docker or kubernetes and --send")
    if not args.send:
        print(json.dumps(collect(args.runtime, args.source_id, args.node, args.cluster), indent=2))
        return
    target = endpoint()  # validate the configuration before collecting anything
    state = Path(args.state_dir or default_state_dir())
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if acquire_lock(state / f"{args.source_id}.lock") is None:
        log("another run for this source holds the lock; skipping")
        return
    spool = Spool(state / f"spool-{args.source_id}", args.spool_max_files, args.spool_max_bytes)

    def deliver(data):
        return send_with_retry(data, lambda d: post(d, target), retries=args.retries)

    def snapshot():
        return collect(args.runtime, args.source_id, args.node, args.cluster)

    if args.watch and args.runtime == "kubernetes":
        watcher = KubeWatcher(time.monotonic(), args.debounce, args.max_delay, args.interval)

        def feed(resource, api_path):
            return lambda items: kube_watch(resource, api_path, items, timeout=args.watch_timeout)

        watch(lambda: collect(args.runtime, args.source_id, pods=watcher.pod_list(), events=[]), spool, deliver,
              watcher, feeds=(feed("pods", "/api/v1/pods"), feed("events", EVENTS_PATH),
                              feed("node-events", NODE_EVENTS_PATH)))
    elif args.watch:
        watch(snapshot, spool, deliver, Watcher(time.monotonic(), args.debounce, args.max_delay, args.interval))
    elif args.runtime == "docker":
        mark = state / f"docker-oom-{args.source_id}.since"
        run_once(lambda: with_docker_ooms(snapshot(), mark), spool, deliver)
    else:
        run_once(snapshot, spool, deliver)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        # Failed collections are never submitted as empty, authoritative snapshots.
        raise SystemExit("Collection/submission failed: " + type(error).__name__)
