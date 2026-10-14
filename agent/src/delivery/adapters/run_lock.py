"""Неблокирующий эксклюзивный flock: запуски агента для одного источника не перекрываются."""
import errno
import fcntl
import os
from pathlib import Path

FILE_MODE = 0o600
BUSY = (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)


class RunLock:
    """Блокировка файла на время жизни процесса."""

    def acquire(self, path: Path) -> int | None:
        """Открытый дескриптор (держится до выхода) или None, если файл заблокирован другим запуском."""
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in BUSY:
                return None
            raise
        return descriptor
