"""Spool неотправленных снимков: отправка по порядку, ограничение по числу файлов и размеру.

Самые старые снимки выбрасываются первыми (сервер все равно игнорирует снимки старше последнего принятого);
выбрасывание отмечается в ошибках добавляемого снимка, чтобы разрыв был виден в портале.
"""
import json
import logging
import time
from collections.abc import Callable

from delivery.adapters.spool_directory import SpoolDirectory
from delivery.entity import BlockedError, JsonObject, RejectedError, RetryableError, SpooledSnapshot, SpoolLimits

logger = logging.getLogger(__name__)

# Место под запись о выброшенных снимках, которая добавляется к новому снимку.
OVERFLOW_NOTE_RESERVE = 128
MIN_FILES = 1

Deliver = Callable[[JsonObject], object]


class SnapshotSpool:
    """Очередь снимков на диске с номером, монотонным даже при переводе часов назад."""

    def __init__(self, directory: SpoolDirectory, limits: SpoolLimits,
                 clock_ns: Callable[[], int] = time.time_ns) -> None:
        """Запоминает каталог, пределы и часы для номеров."""
        self._directory = directory
        self._max_files = max(MIN_FILES, limits.max_files)
        self._max_bytes = limits.max_bytes
        self._clock_ns = clock_ns

    def items(self) -> list:
        """Файлы ожидающих снимков по порядку."""
        items = self._directory.items()
        return items

    def put(self, snapshot: JsonObject) -> None:
        """Добавляет снимок; самый новый сохраняется всегда, даже если один превышает предел размера."""
        files = self._directory.items()
        sequence = max(self._clock_ns(), int(files[-1].stem) + 1 if files else 0)
        payload = json.dumps(snapshot).encode()
        dropped = self._drop_oldest(files, len(payload))
        if dropped:
            logger.warning("spool full: dropped %s oldest unsent snapshot(s)", dropped)
            noted = SpooledSnapshot(snapshot).with_overflow_note(dropped)
            payload = json.dumps(noted.document).encode()
        self._directory.write(sequence, payload)

    def flush(self, deliver: Deliver) -> int:
        """Отправляет снимки по порядку до первого, который сейчас не доставить; возвращает число оставшихся."""
        items = self._directory.items()
        for index, path in enumerate(items):
            try:
                snapshot = self._directory.read(path)
            except (OSError, ValueError):
                logger.exception("dropping unreadable spool file %s", path.name)
                self._directory.remove(path)
                continue
            try:
                deliver(snapshot)
            except RejectedError:
                logger.exception("snapshot %s rejected, dropped", path.name)
            except (RetryableError, BlockedError) as error:
                remaining = len(items) - index
                logger.warning("delivery failed, %s snapshot(s) kept in spool: %s", remaining, error)
                return remaining
            self._directory.remove(path)
        return 0

    def _drop_oldest(self, files: list, payload_size: int) -> int:
        """Удаляет старейшие файлы, пока новый снимок не помещается; после выбрасывания резервирует место записи."""
        sizes = [path.stat().st_size for path in files]
        dropped = 0
        while dropped < len(files) and self._over_limit(files, sizes, dropped, payload_size):
            self._directory.remove(files[dropped])
            dropped += 1
        return dropped

    def _over_limit(self, files: list, sizes: list[int], dropped: int, payload_size: int) -> bool:
        """Превышен ли предел числа файлов или размера с учетом нового снимка."""
        reserve = OVERFLOW_NOTE_RESERVE if dropped else 0
        too_many = len(files) - dropped + 1 > self._max_files
        too_big = sum(sizes[dropped:]) + payload_size + reserve > self._max_bytes
        over = too_many or too_big
        return over
