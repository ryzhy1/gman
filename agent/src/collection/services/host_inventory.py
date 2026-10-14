"""Сведения о хосте Docker-источника для снимка."""
import os

from collection.adapters.host_system import HostSystem
from collection.entity import JsonObject


class HostInventory:
    """Имя хоста и сведения о нем."""

    def __init__(self, system: HostSystem) -> None:
        """Запоминает адаптер хоста."""
        self._system = system

    def hostname(self) -> str:
        """Имя хоста (в контейнере агента — хоста при uts: host)."""
        name = os.uname().nodename
        return name

    def kernel(self) -> str:
        """Версия ядра хоста."""
        release = os.uname().release
        return release

    def facts(self) -> JsonObject:
        """ОС, CPU, память, аптайм, версия Docker и адрес."""
        facts = self._system.facts()
        return facts
