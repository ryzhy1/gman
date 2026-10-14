"""Настройки агента из переменных окружения.

Агент — только stdlib (на хосты ставится одним файлом gman-agent.pyz, нужен лишь python3), поэтому
настройки читаются этим модулем, а не pydantic-settings; окружение читается только здесь.
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT_UID = 0
SYSTEM_STATE_DIR = "/var/lib/gman-agent"
USER_STATE_DIR = ".local/state/gman-agent"
DEFAULT_PROC_ROOT = "/proc"
DEFAULT_KUBERNETES_PORT = "443"
HOST_IP_LIMIT = 100
TRUE = "true"


@dataclass(frozen=True)
class AgentSettings:
    """Конфигурация процесса агента."""

    # Адрес GMan API (GMAN_URL); нужен только для --send.
    gman_url: str
    # Каталог spool и блокировок (GMAN_AGENT_STATE_DIR).
    state_dir: str
    # kubernetes-node: имя узла (NODE_NAME) и source_id кластера (GMAN_CLUSTER) по умолчанию.
    node_name: str
    cluster: str
    # Адрес хоста для портала вместо адреса маршрута по умолчанию (GMAN_HOST_IP).
    host_ip: str
    # /proc хоста; в контейнере нужен hostPID (GMAN_PROC_ROOT).
    proc_root: str
    # API Kubernetes изнутри кластера, когда kubectl не установлен.
    kubernetes_host: str
    kubernetes_port: str
    # Bamboo: неудачная отправка события деплоя валит шаг (GMAN_REQUIRED=true); иначе — только предупреждение.
    deployment_required: bool = False


def load_settings(environ: Mapping[str, str] = os.environ) -> AgentSettings:
    """Читает настройки из окружения; каталог состояния по умолчанию зависит от пользователя."""
    settings = AgentSettings(gman_url=environ.get("GMAN_URL", ""),
                             state_dir=environ.get("GMAN_AGENT_STATE_DIR") or _default_state_dir(),
                             node_name=environ.get("NODE_NAME", ""), cluster=environ.get("GMAN_CLUSTER", ""),
                             host_ip=environ.get("GMAN_HOST_IP", "")[:HOST_IP_LIMIT],
                             proc_root=environ.get("GMAN_PROC_ROOT", DEFAULT_PROC_ROOT),
                             kubernetes_host=environ.get("KUBERNETES_SERVICE_HOST", ""),
                             kubernetes_port=environ.get("KUBERNETES_SERVICE_PORT", DEFAULT_KUBERNETES_PORT),
                             deployment_required=environ.get("GMAN_REQUIRED", "").lower() == TRUE)
    return settings


def _default_state_dir() -> str:
    """/var/lib/gman-agent под root, иначе ~/.local/state/gman-agent."""
    if os.geteuid() == ROOT_UID:
        return SYSTEM_STATE_DIR
    directory = str(Path.home() / USER_STATE_DIR)
    return directory
