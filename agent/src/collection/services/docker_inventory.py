"""Inspect всех контейнеров хоста с отличием удаленного контейнера от отказа демона."""
import subprocess
from typing import Any

from collection.entity import DaemonInspectError
from runtime_access.docker_cli import DockerCli

INSPECT_BATCH = 50


class DockerInventory:
    """Контейнеры хоста; частичный снимок при отказе демона не создается."""

    def __init__(self, docker: DockerCli) -> None:
        """Запоминает Docker CLI."""
        self._docker = docker

    def inspect_containers(self) -> list[dict[str, Any]]:
        """Inspect всех контейнеров; удаленные между `ps` и `inspect` пропускаются."""
        ids = self._docker.container_ids()
        result, missing = [], []
        for start in range(0, len(ids), INSPECT_BATCH):
            batch = ids[start:start + INSPECT_BATCH]
            try:
                result += self._docker.inspect(batch)
            except subprocess.CalledProcessError:
                # Контейнер мог исчезнуть между `ps` и `inspect`: пачка разбирается по одному.
                found, gone = self._inspect_each(batch)
                result += found
                missing += gone
        if missing and set(missing) & set(self._docker.container_ids()):
            # Контейнер все еще в списке, но не осматривается: отказ демона, а не удаление.
            raise DaemonInspectError("docker inspect failed for existing containers")
        return result

    def inspect_one(self, container_id: str) -> dict[str, Any] | None:
        """Inspect одного контейнера или None, если он уже удален (например, `docker run --rm`)."""
        try:
            containers = self._docker.inspect([container_id])
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
        if not containers:
            return None
        container = containers[0]
        return container

    def _inspect_each(self, batch: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        """Inspect по одному контейнеру: найденные и пропавшие."""
        found, missing = [], []
        for container_id in batch:
            try:
                found += self._docker.inspect([container_id])
            except subprocess.CalledProcessError:
                missing.append(container_id)
        result = (found, missing)
        return result
