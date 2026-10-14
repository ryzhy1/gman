"""GPUs (NVIDIA via nvidia-smi, AMD via sysfs) and the containers/pods of GPU processes by cgroup."""
import csv
import os
import re
import subprocess
from pathlib import Path

from . import common

# Host /proc. In a container this requires hostPID: nvidia-smi reports PIDs of the caller's PID namespace.
PROC_ROOT = os.environ.get("GMAN_PROC_ROOT", "/proc")


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
        return list(csv.reader(common.command(["nvidia-smi", "--query-gpu=" + names, "--format=csv,noheader,nounits"]).splitlines()))
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
        apps = common.command(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"])
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
