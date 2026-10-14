"""Общие фикстуры тестов агента: подмена запуска команд и сборка модулей без реального runtime."""
import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from collection.container import build_collection_orchestrator, collection_target
from collection.orchestrator import CollectionOrchestrator
from config.settings import AgentSettings
from runtime_access.command_runner import CommandRunner
from runtime_access.connection import RuntimeAccess, connect_runtime

ROOT = Path(__file__).resolve().parents[2]
BACKEND_SCHEMAS = ROOT / "backend/src/api/schemas"


class FakeRunner(CommandRunner):
    """Запуск команд, отвечающий функцией handler(args) -> stdout (или исключением)."""

    def __init__(self, handler: Callable[[list[str]], str] | None = None,
                 run: Callable[..., subprocess.CompletedProcess] | None = None) -> None:
        super().__init__()
        self.handler = handler or (lambda args: (_ for _ in ()).throw(FileNotFoundError(args[0])))
        self.run_handler = run
        self.calls: list[list[str]] = []

    def output(self, args: list[str]) -> str:
        self.calls.append(args)
        return self.handler(args)

    def run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        if self.run_handler is None:
            return subprocess.CompletedProcess(args, 1, b"")
        return self.run_handler(args, capture_output=True, timeout=timeout)


def settings(**overrides: object) -> AgentSettings:
    """Настройки агента без чтения окружения процесса тестов."""
    values = {"gman_url": "", "state_dir": "/nonexistent", "node_name": "", "cluster": "", "host_ip": "",
              "proc_root": "/proc", "kubernetes_host": "", "kubernetes_port": "443", **overrides}
    return AgentSettings(**values)


def runtime(handler: Callable[[list[str]], str] | None = None, agent_settings: AgentSettings | None = None,
            **kwargs: object) -> RuntimeAccess:
    """Доступ к runtime поверх FakeRunner; kwargs — подмены urlopen/ssl/Popen."""
    return connect_runtime(agent_settings or settings(), FakeRunner(handler), **kwargs)


def collection(runtime_name: str = "docker", source_id: str = "node-1", node: str = "", cluster: str = "",
               handler: Callable[[list[str]], str] | None = None, access: RuntimeAccess | None = None,
               drm_root: Path = Path("/nonexistent"), **setting_overrides: object) -> CollectionOrchestrator:
    """Оркестратор collection над поддельным runtime (AMD sysfs по умолчанию отключен)."""
    agent_settings = settings(**setting_overrides)
    target = collection_target(agent_settings, runtime_name, source_id, node, cluster)
    return build_collection_orchestrator(agent_settings, target, access or runtime(handler, agent_settings),
                                         drm_root=drm_root)


@pytest.fixture(scope="session")
def api_schemas():
    """HTTP-модели снимка GMan API: вывод агента должен им соответствовать (проверка контракта)."""
    pytest.importorskip("pydantic")
    spec = importlib.util.spec_from_file_location("gman_api_schemas", BACKEND_SCHEMAS / "__init__.py",
                                                  submodule_search_locations=[str(BACKEND_SCHEMAS)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["gman_api_schemas"] = package
    spec.loader.exec_module(package)
    return importlib.import_module("gman_api_schemas.inventory")
