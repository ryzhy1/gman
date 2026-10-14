"""Файл-отметка: до какого времени прочитаны события OOM Docker (между запусками по таймеру)."""
import os
from pathlib import Path

TEMPORARY_SUFFIX = ".tmp"


class OomMarkStore:
    """Хранит Unix-время последнего прочитанного периода в файле состояния."""

    def read(self, mark: Path) -> int | None:
        """Сохраненное время или None, если отметки нет или она повреждена."""
        try:
            value = int(mark.read_text().strip())
        except (OSError, ValueError):
            return None
        return value

    def write(self, mark: Path, value: int) -> None:
        """Атомарно записывает время."""
        temporary = Path(str(mark) + TEMPORARY_SUFFIX)
        temporary.write_text(str(value))
        os.replace(temporary, mark)
