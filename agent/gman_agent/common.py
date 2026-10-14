"""Helpers shared by the collectors."""
import re
import subprocess
import sys
from datetime import datetime

AGENT_VERSION = "0.7.0"

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
# GPU device selection variables, also reported with the GPU requests: they hold GPU indexes/UUIDs.
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


def log(message):
    print(f"gman-agent: {message}", file=sys.stderr, flush=True)
