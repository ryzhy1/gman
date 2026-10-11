"""Inventory state as Prometheus metrics for alert rules (deploy/alerts/rules.yaml).

Served on /metrics of the API container. It is scraped by Prometheus inside the Compose network and
is not proxied by nginx (only /api/ and /grafana/ are), so it is not reachable from outside the server.
Values come from the last accepted snapshot of each source. When a source goes silent its instance series
stay at the last values and GManSourceSilent inhibits the alerts derived from them; GPU readings of a silent
source are dropped instead, so that a stale temperature does not compete with live DCGM data.

Label conventions match the telemetry pipeline: host_id is the Docker host's source_id (GMAN_HOST_ID)
or the Kubernetes node name, UUID is the NVIDIA GPU UUID (as in DCGM Exporter).
"""
from datetime import datetime, timezone

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from . import db as store

# Reasons safe to use as label values: a fixed set keeps cardinality bounded and drops free-form errors.
STATE_REASONS = {"OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
                 "CreateContainerError", "RunContainerError", "ContainerCannotRun", "Error", "Completed"}
# Same bits as nvidia-smi clocks_throttle_reasons and DCGM_FI_DEV_CLOCKS_EVENT_REASONS (agent/collect.py).
# Same threshold as GManSourceSilent: readings older than this are not exported as current GPU state.
GPU_FRESH_SECONDS = 300
THROTTLE_BITS = {"gpu_idle": 0x1, "applications_clocks": 0x2, "sw_power_cap": 0x4, "hw_slowdown": 0x8,
                 "sync_boost": 0x10, "sw_thermal": 0x20, "hw_thermal": 0x40, "hw_power_brake": 0x80,
                 "display_clocks": 0x100}

HELP = {
    "gman_source_last_snapshot_timestamp_seconds": ("gauge", "Server receive time of the last accepted snapshot."),
    "gman_source_collect_errors": ("gauge", "Partial collection errors reported in the last snapshot."),
    "gman_k8s_node_ready": ("gauge", "Kubernetes node Ready condition from the last snapshot (1 ready, 0 not)."),
    "gman_instance_restarts_total": ("counter", "Restart count of the container as reported by the runtime."),
    "gman_instance_state_reason": ("gauge", "1 for the current state reason of the container (allowlisted)."),
    "gman_instance_last_termination_reason": ("gauge", "1 for the reason of the previous termination (Kubernetes)."),
    "gman_instance_last_oom_timestamp_seconds": ("gauge", "Time of the latest OOM kill of the container (Docker oom "
                                                          "event or OOMKilled, Kubernetes termination)."),
    "gman_gpu_temperature_celsius": ("gauge", "GPU core temperature from nvidia-smi."),
    "gman_gpu_ecc_uncorrected_errors": ("gauge", "Volatile uncorrected ECC errors from nvidia-smi."),
    "gman_gpu_clock_event_reasons": ("gauge", "Active clock throttle reasons from nvidia-smi as a DCGM bitmask."),
    "gman_release_status": ("gauge", "1 for the status of the latest release of a service/environment (releases.py)."),
    "gman_release_started_timestamp_seconds": ("gauge", "Start of the latest release of a service/environment."),
}


def latest_releases(session):
    """Latest release per service/environment: one series each, whatever the release history length."""
    newer = aliased(store.Release)
    release = store.Release
    return session.scalars(select(release).where(~exists().where(
        newer.service_id == release.service_id, newer.environment == release.environment,
        or_(newer.started_at > release.started_at,
            and_(newer.started_at == release.started_at, newer.id > release.id))))
        .order_by(release.service_id, release.environment)).all()


def escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def line(name, labels, value):
    rendered = ",".join(f'{key}="{escape(item)}"' for key, item in labels.items())
    return f"{name}{{{rendered}}} {float(value)!r}"


def render(session):
    samples = {name: [] for name in HELP}

    def add(name, labels, value):
        samples[name].append(line(name, labels, value))

    now = datetime.now(timezone.utc)
    for source in session.scalars(select(store.Source).order_by(store.Source.source_id)):
        # On a Docker host GMAN_HOST_ID equals the source_id; a node-local GPU source ("<cluster>.<node>")
        # reports for the node in its hostname, as DCGM and the k8s Collector label it; a cluster has no
        # single host. The shared host_id lets GManK8sNodeNotReady inhibit the node source's alerts and lets
        # DCGM series replace the nvidia-smi ones in the recording rules instead of duplicating the GPU.
        host_id = {"docker-host": source.source_id, "kubernetes-node": source.hostname}.get(source.source_type)
        base = {"source_id": source.source_id, "source_type": source.source_type}
        add("gman_source_last_snapshot_timestamp_seconds", {**base, **({"host_id": host_id} if host_id else {})},
            source.received_at.timestamp())
        snapshot = source.snapshot or {}
        add("gman_source_collect_errors", base, len(snapshot.get("errors") or []))
        for node in snapshot.get("nodes") or []:
            add("gman_k8s_node_ready", {"source_id": source.source_id, "host_id": node["name"]}, 1 if node["ready"] else 0)
        fresh = (now - source.received_at).total_seconds() <= GPU_FRESH_SECONDS
        for gpu in (snapshot.get("gpus") or []) if fresh else []:
            if gpu.get("vendor") != "NVIDIA":
                continue
            labels = {"source_id": source.source_id, "host_id": host_id or source.source_id, "UUID": gpu["id"],
                      "model": gpu.get("model", "")}
            if gpu.get("temperature_c") is not None:
                add("gman_gpu_temperature_celsius", labels, gpu["temperature_c"])
            if gpu.get("ecc_uncorrected_errors") is not None:
                add("gman_gpu_ecc_uncorrected_errors", labels, gpu["ecc_uncorrected_errors"])
            if "throttle_reasons" in gpu:
                add("gman_gpu_clock_event_reasons", labels,
                    sum(THROTTLE_BITS.get(reason, 0) for reason in set(gpu["throttle_reasons"])))
    for row in session.scalars(select(store.Instance).order_by(store.Instance.source_id, store.Instance.position)):
        item = row.data
        # A pod's host is its node (empty while unscheduled); a container's host is its Docker host.
        host = item.get("node", "") if item.get("runtime") == "kubernetes" else row.source_id
        labels = {"source_id": row.source_id, "host_id": host, "instance_id": row.instance_id,
                  "instance_name": item.get("name", ""), "namespace": item.get("namespace", ""),
                  "service_id": item.get("service_id", ""), "environment": item.get("environment", ""),
                  "runtime": item.get("runtime", "")}
        add("gman_instance_restarts_total", labels, item.get("restarts") or 0)
        if item.get("reason") in STATE_REASONS:
            add("gman_instance_state_reason", {**labels, "reason": item["reason"]}, 1)
        last = (item.get("last_termination_reason") or "").split(" ")[0]
        if last in STATE_REASONS:
            add("gman_instance_last_termination_reason", {**labels, "reason": last}, 1)
        if item.get("last_oom_at"):
            # reason="OOMKilled" gives the series the label set of gman_instance_state_reason, so that
            # GManInstanceOOMKilled raises one alert when both report the same kill.
            add("gman_instance_last_oom_timestamp_seconds", {**labels, "reason": "OOMKilled"},
                datetime.fromisoformat(item["last_oom_at"]).timestamp())

    for release in latest_releases(session):
        labels = {"service_id": release.service_id, "environment": release.environment}
        add("gman_release_status", {**labels, "status": release.status}, 1)
        add("gman_release_started_timestamp_seconds", labels, release.started_at.timestamp())

    output = []
    for name, (kind, text) in HELP.items():
        output += [f"# HELP {name} {text}", f"# TYPE {name} {kind}", *samples[name]]
    return "\n".join(output) + "\n"
