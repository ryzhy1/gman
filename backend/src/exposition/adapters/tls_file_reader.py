"""Чтение наблюдаемых TLS-файлов (сертификаты и CRL nginx, смонтированные только для чтения)."""
import os

FileStamp = tuple[int, int, int]


class TlsFileReader:
    """Отметка изменения файла и его содержимое с ограничением размера."""

    def __init__(self, max_bytes: int) -> None:
        """Запоминает предел читаемого размера: сертификаты и CRL малы, большой файл — ошибка конфигурации."""
        self._max_bytes = max_bytes

    def stamp(self, path: str) -> FileStamp:
        """(mtime_ns, size, inode): по ней видно, заменен ли файл."""
        info = os.stat(path)
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        return stamp

    def read(self, path: str) -> bytes:
        """Содержимое файла (не больше предела)."""
        with open(path, "rb") as handle:
            data = handle.read(self._max_bytes)
        return data
