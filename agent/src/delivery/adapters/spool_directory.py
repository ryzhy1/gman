"""Каталог spool: по JSON-файлу на снимок с именем-номером, атомарная запись."""
import json
import os
from pathlib import Path

from delivery.entity import JsonObject

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
TEMPORARY_SUFFIX = ".tmp"
SNAPSHOT_SUFFIX = ".json"
SEQUENCE_WIDTH = 20


class SpoolDirectory:
    """Файлы неотправленных снимков; недописанные .tmp прошлых запусков удаляются при открытии."""

    def __init__(self, directory: Path) -> None:
        """Создает каталог (только для владельца) и убирает недописанные файлы."""
        self._directory = directory
        self._directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        for stale in self._directory.glob("*" + TEMPORARY_SUFFIX):
            stale.unlink(missing_ok=True)

    def items(self) -> list[Path]:
        """Файлы снимков по возрастанию номера."""
        files = sorted(path for path in self._directory.glob("*" + SNAPSHOT_SUFFIX) if path.stem.isdigit())
        return files

    def write(self, sequence: int, payload: bytes) -> Path:
        """Атомарно записывает снимок с номером (fsync до переименования)."""
        path = self._directory / f"{sequence:0{SEQUENCE_WIDTH}d}{SNAPSHOT_SUFFIX}"
        temporary = path.with_suffix(TEMPORARY_SUFFIX)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path

    def read(self, path: Path) -> JsonObject:
        """Снимок из файла; поврежденный файл — OSError или ValueError."""
        snapshot = json.loads(path.read_bytes())
        return snapshot

    def remove(self, path: Path) -> None:
        """Удаляет файл снимка."""
        path.unlink(missing_ok=True)
