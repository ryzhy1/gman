"""Логирование агента: сообщения в stderr с префиксом gman-agent (stdout занят JSON-снимком)."""
import logging
import sys

LOG_FORMAT = "gman-agent: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Настраивает корневой логгер процесса агента."""
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter(LOG_FORMAT)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
