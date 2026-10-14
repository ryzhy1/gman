"""Подключение к runtime: один запуск команд, Docker CLI и API Kubernetes на процесс агента."""
import ssl
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from urllib.request import urlopen

from config.settings import AgentSettings
from runtime_access.command_runner import CommandRunner
from runtime_access.docker_cli import DockerCli
from runtime_access.kubernetes_api import InClusterAccess, KubernetesApi, Opener


@dataclass(frozen=True)
class RuntimeAccess:
    """Адаптеры доступа к runtime, общие для модулей collection и watching."""

    runner: CommandRunner
    docker: DockerCli
    kubernetes: KubernetesApi


def connect_runtime(settings: AgentSettings, runner: CommandRunner | None = None, opener: Opener = urlopen,
                    context_factory: Callable[..., ssl.SSLContext] = ssl.create_default_context,
                    popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> RuntimeAccess:
    """Создает адаптеры; низкоуровневые функции подменяются в тестах."""
    commands = runner or CommandRunner()
    docker = DockerCli(commands)
    in_cluster = InClusterAccess(host=settings.kubernetes_host, port=settings.kubernetes_port)
    kubernetes = KubernetesApi(commands, in_cluster, opener, context_factory, popen)
    access = RuntimeAccess(runner=commands, docker=docker, kubernetes=kubernetes)
    return access
