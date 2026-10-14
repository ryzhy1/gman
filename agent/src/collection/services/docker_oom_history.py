"""OOM-kill Docker без --watch: события `oom` с прошлого запуска по таймеру как last_oom_at.

Docker сбрасывает State.OOMKilled, когда политика перезапуска перезапускает контейнер, а OOM-kill дочернего
процесса не останавливает контейнер вовсе; событие `oom` остается в буфере событий демона. API хранит
последний last_oom_at после записи. У удаленных контейнеров (--rm) нет экземпляра, чтобы его нести.
"""
import subprocess
from collections.abc import Callable
from pathlib import Path

from collection.adapters.oom_mark_store import OomMarkStore
from collection.entity import Instance, RuntimeTimestamp
from runtime_access.docker_cli import DockerCli

# Демон хранит в памяти только последние события (несколько сотен): дальше в прошлое искать бесполезно.
OOM_LOOKBACK_SECONDS = 3600


class DockerOomHistory:
    """Время последнего OOM контейнеров по событиям демона с отметки прошлого запуска."""

    def __init__(self, docker: DockerCli, marks: OomMarkStore, clock: Callable[[], float],
                 lookback: int = OOM_LOOKBACK_SECONDS) -> None:
        """Запоминает Docker CLI, хранилище отметки, часы и глубину поиска."""
        self._docker = docker
        self._marks = marks
        self._clock = clock
        self._lookback = lookback

    def apply(self, instances: list[Instance], errors: list[str], mark: Path) -> None:
        """Добавляет время OOM к экземплярам; при ошибке отметка остается — следующий запуск повторит период."""
        until = int(self._clock())
        since = max(self._marks.read(mark) or 0, until - self._lookback)
        if since >= until:
            return
        try:
            ooms = self.ooms(since, until)
        except (OSError, subprocess.SubprocessError) as error:
            errors.append("docker oom events unavailable: " + type(error).__name__)
            return
        for instance in instances:
            if instance.id in ooms:
                instance.record_oom(ooms[instance.id])
        self._marks.write(mark, until)

    def ooms(self, since: int, until: int) -> dict[str, str]:
        """ID контейнера -> время последнего события `oom` за период."""
        result: dict[str, str] = {}
        for event in self._docker.oom_events(since, until):
            if event.container_id and event.time:
                result[event.container_id] = RuntimeTimestamp.newest(result.get(event.container_id), event.time)
        return result
