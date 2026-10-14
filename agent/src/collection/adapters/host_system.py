"""Хост агента: ОС, ядро, CPU, память, аптайм, версия Docker и адрес."""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from collection.entity import JsonObject
from runtime_access.command_runner import CommandRunner
from runtime_access.docker_cli import DockerCli

DARWIN = "darwin"
KIB = 1024
# UDP connect не отправляет пакетов: он только выбирает адрес маршрута по умолчанию.
ROUTE_PROBE = ("10.255.255.255", 1)
BOOT_TIME = re.compile(r"sec\s*=\s*(\d+)")


class HostSystem:
    """Сведения о хосте из /etc, /proc, sysctl (macOS) и Docker."""

    def __init__(self, runner: CommandRunner, docker: DockerCli, host_ip: str, root: Path = Path("/")) -> None:
        """Запоминает запуск команд, Docker CLI, заданный адрес хоста и корень файловой системы."""
        self._runner = runner
        self._docker = docker
        self._host_ip = host_ip
        self._root = root

    def facts(self) -> JsonObject:
        """Сведения о хосте; недоступное сведение не указывается."""
        info: JsonObject = {"kernel": os.uname().release, "cpu_count": os.cpu_count()}
        if sys.platform == DARWIN:
            # Docker Desktop держит контейнеры в VM; inventory описывает Mac, где работает агент.
            info.update(self._macos_facts())
        info.update(self._linux_facts())
        try:
            info["runtime_version"] = "docker " + self._docker.server_version()
        except (OSError, subprocess.SubprocessError):
            pass
        info["ip"] = self.host_ip()
        return info

    def host_ip(self) -> str:
        """Адрес маршрута по умолчанию — где работает сервис для портала; GMAN_HOST_IP его заменяет."""
        if self._host_ip:
            return self._host_ip
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(ROUTE_PROBE)
                address = probe.getsockname()[0]
        except OSError:
            return ""
        return address

    def _macos_facts(self) -> JsonObject:
        """Память, CPU, версия macOS и аптайм через sysctl и sw_vers."""
        facts: JsonObject = {}
        commands = {"memory_total_bytes": ["sysctl", "-n", "hw.memsize"],
                    "cpu_model": ["sysctl", "-n", "machdep.cpu.brand_string"],
                    "os": ["sw_vers", "-productVersion"], "uptime_seconds": ["sysctl", "-n", "kern.boottime"]}
        for key, args in commands.items():
            try:
                facts[key] = _parse_macos(key, self._runner.output(args))
            except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
                continue
        return facts

    def _linux_facts(self) -> JsonObject:
        """ОС, модель CPU, аптайм и память из /etc/os-release и /proc."""
        facts: JsonObject = {}
        try:
            for line in (self._root / "etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    facts["os"] = line.split("=", 1)[1].strip('"')
            cpuinfo = (self._root / "proc/cpuinfo").read_text().splitlines()
            facts["cpu_model"] = next((line.split(":", 1)[1].strip() for line in cpuinfo
                                       if line.startswith("model name")), "")
            facts["uptime_seconds"] = int(float((self._root / "proc/uptime").read_text().split()[0]))
            facts["memory_total_bytes"] = _memory_total(self._root / "proc/meminfo")
        except OSError:
            pass
        return facts


def _parse_macos(key: str, output: str) -> object:
    """Значение сведения macOS из вывода команды."""
    if key == "memory_total_bytes":
        value = int(output)
    elif key == "uptime_seconds":
        value = max(0, int(time.time()) - int(BOOT_TIME.search(output).group(1)))
    elif key == "os":
        value = "macOS " + output.strip()
    else:
        value = output.strip()
    return value


def _memory_total(meminfo: Path) -> int | None:
    """Объем памяти из MemTotal (КиБ)."""
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) * KIB
            return total
    return None
