"""Настройка логирования процесса API."""
import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    """Настраивает корневой логгер: уровень и формат сообщений."""
    logging.basicConfig(level=level, format=LOG_FORMAT)
