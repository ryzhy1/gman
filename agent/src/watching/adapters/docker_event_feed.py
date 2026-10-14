"""Поток `docker events` в очередь --watch с переподключением; None в очереди отмечает разрыв потока."""
import logging
import queue
import time
from collections.abc import Callable

from runtime_access.docker_cli import DockerCli, parse_event

logger = logging.getLogger(__name__)

RECONNECT_SECONDS = 5.0


class DockerEventFeed:
    """Читает события контейнеров и кладет их в очередь как ContainerEvent."""

    def __init__(self, docker: DockerCli, reconnect: float = RECONNECT_SECONDS,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """Запоминает Docker CLI, паузу перед переподключением и функцию ожидания."""
        self._docker = docker
        self._reconnect = reconnect
        self._sleep = sleep

    def run(self, sink: queue.Queue, restarts: int | None = None) -> None:
        """Читает поток до разрыва и переподключается; restarts ограничивает число подключений в тестах."""
        while restarts is None or restarts > 0:
            self._read(sink)
            # События могли потеряться, пока потока не было: нужна сверка полным снимком.
            sink.put(None)
            if restarts is not None:
                restarts -= 1
            self._sleep(self._reconnect)

    def _read(self, sink: queue.Queue) -> None:
        """Одно подключение к `docker events`; неразборчивые строки пропускаются."""
        try:
            for line in self._docker.event_stream():
                if not line.strip():
                    continue
                event = parse_event(line)
                if event is None:
                    logger.warning("ignoring malformed docker event")
                    continue
                sink.put(event)
        except OSError as error:
            logger.warning("docker events unavailable: %s", type(error).__name__)
