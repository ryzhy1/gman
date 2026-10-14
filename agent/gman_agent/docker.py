"""Docker containers: inspect output as inventory instances, dotenv files, events and OOM kills."""
import copy
import io
import json
import os
import re
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import common
from .common import GPU_ENV, duration, log, newest, origin, timestamp


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
    # Every variable as "NAME=value", including values that may be secrets (image ENV and docker run -e).
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
                       "env": list(cfg.get("Env") or []),
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


# Dotenv files an application may load itself at start (godotenv, python-dotenv, dotenv for Node...): their
# variables never reach the container environment, so they are read from the container's working directory.
ENV_FILES = (".env", ".env.local", ".env.development", ".env.dev", ".env.production", ".env.prod", ".env.staging",
             ".env.test")
ENV_FILE_MAX_BYTES = 256 * 1024
DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*?)\s*$")
# (container ID, StartedAt) -> files: applications read them at start, so a running container is read once.
env_file_cache = {}


def parse_dotenv(text):
    """"NAME=value" per assignment in file order; comments, blank lines and quotes around the value dropped."""
    result = []
    for line in text.splitlines():
        match = DOTENV_LINE.match(line)
        if not match:
            continue
        name, value = match[1], match[2]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        result.append(f"{name}={value}")
    return result


def read_container_file(container_id, path, run=subprocess.run):
    """A regular file from a container (running or stopped) via `docker cp`, without exec; None if absent."""
    try:
        done = run(["docker", "cp", f"{container_id}:{path}", "-"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
            member = next((m for m in archive if m.isfile()), None)
            if member is None or member.size > ENV_FILE_MAX_BYTES:
                return None
            return archive.extractfile(member).read().decode(errors="replace")
    except (tarfile.TarError, OSError):
        return None


def env_files(c, read=read_container_file):
    """Dotenv files in the working directory of an inspected container: {path: ["NAME=value", ...]}."""
    key = (c.get("Id", ""), (c.get("State") or {}).get("StartedAt", ""))
    if key not in env_file_cache:
        directory = ((c.get("Config") or {}).get("WorkingDir") or "/").rstrip("/")
        found = {}
        for name in ENV_FILES:
            text = read(c.get("Id", ""), f"{directory}/{name}")
            if text is not None:
                found[f"{directory}/{name}"] = parse_dotenv(text)
        if len(env_file_cache) > 1000:
            env_file_cache.clear()
        env_file_cache[key] = found
    return env_file_cache[key]


def docker_instance_with_files(c):
    instance = docker_instance(c)
    instance["config"]["env_files"] = env_files(c)
    return instance


def inspect_containers():
    ids = common.command(["docker", "ps", "-aq", "--no-trunc"]).split()
    result, missing = [], []
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        try:
            result.extend(json.loads(common.command(["docker", "inspect", *batch])))
        except subprocess.CalledProcessError:
            # A container can be removed between `ps` and `inspect`; isolate it.
            for cid in batch:
                try:
                    result.extend(json.loads(common.command(["docker", "inspect", cid])))
                except subprocess.CalledProcessError:
                    missing.append(cid)
    if missing and set(missing) & set(common.command(["docker", "ps", "-aq", "--no-trunc"]).split()):
        # Still listed but not inspectable: the daemon is failing, not the container gone.
        raise RuntimeError("docker inspect failed for existing containers")
    return result


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
        return docker_instance_with_files(json.loads(common.command(["docker", "inspect", event_id(event)]))[0])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError, KeyError):
        return event_instance(event, previous)


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


# --- Docker OOM without --watch -----------------------------------------------------------------------

# The daemon keeps only its latest events in memory (a few hundred): a longer look-back finds nothing more.
OOM_LOOKBACK = 3600


def docker_ooms(since, until, run=common.command):
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


def with_docker_ooms(data, mark, clock=time.time, run=common.command):
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
