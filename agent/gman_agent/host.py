"""The host the agent runs on: OS, kernel, CPU, memory, uptime, Docker version and address."""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import common


def host_info():
    info = {"kernel": os.uname().release, "cpu_count": os.cpu_count()}
    if sys.platform == "darwin":
        # Docker Desktop runs containers in a VM; inventory describes the Mac running this agent.
        fields = [
            ("memory_total_bytes", ["sysctl", "-n", "hw.memsize"], int),
            ("cpu_model", ["sysctl", "-n", "machdep.cpu.brand_string"], str.strip),
            ("os", ["sw_vers", "-productVersion"], lambda s: "macOS " + s.strip()),
            ("uptime_seconds", ["sysctl", "-n", "kern.boottime"],
             lambda s: max(0, int(time.time()) - int(re.search(r"sec\s*=\s*(\d+)", s).group(1)))),
        ]
        for key, args, parse in fields:
            try:
                info[key] = parse(common.command(args))
            except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
                pass
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
        info["runtime_version"] = "docker " + common.command(["docker", "version", "--format", "{{.Server.Version}}"]).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    info["ip"] = host_ip()
    return info


def host_ip():
    """Address of the default route, as the portal shows where a service runs; GMAN_HOST_IP overrides it
    (e.g. a public or NAT address). A UDP connect sends no packets."""
    if os.environ.get("GMAN_HOST_IP"):
        return os.environ["GMAN_HOST_IP"][:100]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return ""
