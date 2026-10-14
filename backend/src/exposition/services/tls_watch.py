"""Наблюдение за TLS-файлами из GMAN_TLS_WATCH_FILES: разобранное содержимое с кешем по отметке файла."""
import logging
import threading

from cryptography.exceptions import UnsupportedAlgorithm

from exposition.adapters.tls_file_reader import FileStamp, TlsFileReader
from exposition.entity import TlsContents
from exposition.services.tls_parser import TlsParser

logger = logging.getLogger(__name__)

# Пути с "key" не открываются: ошибка в списке не должна заставить API читать закрытый ключ.
KEY_MARKER = "key"


class TlsWatch:
    """Разбирает наблюдаемые файлы; повторно читает только замененные (кеш по mtime, размеру и inode)."""

    def __init__(self, paths: list[str], reader: TlsFileReader, parser: TlsParser) -> None:
        """Запоминает список файлов, адаптер чтения и разборщик."""
        self._paths = paths
        self._reader = reader
        self._parser = parser
        self._cache: dict[str, tuple[FileStamp, TlsContents]] = {}
        self._lock = threading.Lock()

    @property
    def paths(self) -> list[str]:
        """Наблюдаемые файлы в порядке конфигурации."""
        paths = list(self._paths)
        return paths

    def contents(self, path: str) -> TlsContents | None:
        """Содержимое файла или None при любой ошибке (это gman_tls_file_error, а не упавший scrape)."""
        if KEY_MARKER in path.lower():
            logger.warning("GMAN_TLS_WATCH_FILES: %s looks like a private key, not read", path)
            return None
        try:
            contents = self._read(path)
        except (OSError, ValueError, UnsupportedAlgorithm):
            # Нечитаемый файл — метрика gman_tls_file_error, а не упавший scrape.
            logger.exception("GMAN_TLS_WATCH_FILES: cannot read %s", path)
            with self._lock:
                self._cache.pop(path, None)
            return None
        return contents

    def _read(self, path: str) -> TlsContents:
        """Содержимое из кеша, если файл не менялся, иначе разобранное заново."""
        stamp = self._reader.stamp(path)
        with self._lock:
            cached = self._cache.get(path)
        if cached and cached[0] == stamp:
            contents = cached[1]
            return contents
        data = self._reader.read(path)
        contents = self._parser.parse(data)
        with self._lock:
            self._cache[path] = (stamp, contents)
        return contents
