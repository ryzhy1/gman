"""Docker CLI: список и inspect контейнеров, чтение файлов контейнера, события и версия демона."""
import io
import json
import logging
import subprocess
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from runtime_access.command_runner import CommandRunner

logger = logging.getLogger(__name__)

NANOSECONDS = 10 ** 9
COPY_TIMEOUT_SECONDS = 10
CONTAINER_TYPE = "container"
DEFAULT_ACTION = ""


@dataclass(frozen=True)
class ContainerEvent:
    """Событие контейнера из `docker events`."""

    container_id: str
    action: str
    time: str | None
    attributes: dict[str, str] = field(default_factory=dict)
    type: str = CONTAINER_TYPE


class DockerCli:
    """Обращения к демону Docker через CLI (сокет монтируется в контейнер агента только на чтение)."""

    def __init__(self, runner: CommandRunner) -> None:
        """Запоминает запуск команд."""
        self._runner = runner

    def container_ids(self) -> list[str]:
        """ID всех контейнеров, включая остановленные."""
        ids = self._runner.output(["docker", "ps", "-aq", "--no-trunc"]).split()
        return ids

    def inspect(self, ids: list[str]) -> list[dict[str, Any]]:
        """Вывод docker inspect; несуществующий контейнер — CalledProcessError."""
        output = self._runner.output(["docker", "inspect", *ids])
        containers = json.loads(output)
        return containers

    def read_file(self, container_id: str, path: str, max_bytes: int) -> str | None:
        """Обычный файл контейнера (работающего или остановленного) через `docker cp`, без exec; иначе None."""
        try:
            done = self._runner.run(["docker", "cp", f"{container_id}:{path}", "-"], COPY_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        text = _single_file(done.stdout, max_bytes)
        return text

    def oom_events(self, since: int, until: int) -> list[ContainerEvent]:
        """События `oom` между двумя Unix-временами (`docker events` завершается на --until)."""
        output = self._runner.output(["docker", "events", "--since", str(since), "--until", str(until),
                                      "--filter", "type=container", "--filter", "event=oom",
                                      "--format", "{{json .}}"])
        events = []
        for line in output.splitlines():
            event = parse_event(line)
            if event is not None:
                events.append(event)
        return events

    def event_stream(self) -> Iterator[str]:
        """Строки `docker events` контейнеров, пока поток не прервется."""
        process = self._runner.stream(["docker", "events", "--format", "{{json .}}", "--filter", "type=container"])
        for line in process.stdout:
            yield line
        process.wait()

    def server_version(self) -> str:
        """Версия демона Docker."""
        version = self._runner.output(["docker", "version", "--format", "{{.Server.Version}}"]).strip()
        return version


def parse_event(line: str) -> ContainerEvent | None:
    """Событие из JSON-строки `docker events`; неразборчивая строка — None."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    actor = raw.get("Actor") or {}
    action = (raw.get("Action") or raw.get("status") or DEFAULT_ACTION).split(":")[0].strip()
    event = ContainerEvent(container_id=actor.get("ID") or raw.get("id") or "", action=action,
                           time=_event_time(raw), attributes=actor.get("Attributes") or {},
                           type=raw.get("Type", CONTAINER_TYPE))
    return event


def _event_time(raw: dict[str, Any]) -> str | None:
    """Время события в ISO-формате по timeNano (или time)."""
    nanoseconds = raw.get("timeNano") or int(raw.get("time") or 0) * NANOSECONDS
    if not nanoseconds:
        return None
    moment = datetime.fromtimestamp(nanoseconds / NANOSECONDS, timezone.utc).isoformat()
    return moment


def _single_file(archive_bytes: bytes, max_bytes: int) -> str | None:
    """Содержимое первого обычного файла tar-архива `docker cp`, если он не больше предела."""
    buffer = io.BytesIO(archive_bytes)
    try:
        with tarfile.open(fileobj=buffer) as archive:
            member = next((item for item in archive if item.isfile()), None)
            if member is None or member.size > max_bytes:
                return None
            text = archive.extractfile(member).read().decode(errors="replace")
    except (tarfile.TarError, OSError):
        return None
    return text
