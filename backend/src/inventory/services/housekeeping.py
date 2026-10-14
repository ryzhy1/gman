"""Обслуживание после принятых снимков (отдельного демона нет): тайм-ауты релизов и очистка истории."""
import logging
from datetime import datetime, timedelta

from database.unit_of_work import StorageError
from inventory.adapters.retention_purger import RetentionPurger
from inventory.ports import ReleaseTracker

logger = logging.getLogger(__name__)


class Housekeeping:
    """Не чаще одного раза за интервал в процессе: тайм-ауты релизов и удаление истории старше срока."""

    def __init__(self, purger: RetentionPurger, releases: ReleaseTracker, retention_days: int,
                 purge_interval: timedelta, expiry_interval: timedelta) -> None:
        """Запоминает адаптер очистки, учет релизов, срок хранения и интервалы запусков."""
        self._purger = purger
        self._releases = releases
        self._retention_days = retention_days
        self._purge_interval = purge_interval
        self._expiry_interval = expiry_interval
        self._last_purge: datetime | None = None
        self._last_expiry: datetime | None = None

    def run(self, now: datetime) -> bool:
        """Выполняет то, чему пришло время; True — история очищалась. Ошибки только логируются.

        Обслуживание не должно превращать уже зафиксированный снимок в ошибку для агента.
        """
        try:
            self._expire_releases(now)
            purged = self._purge(now)
        except StorageError:
            logger.exception("Housekeeping failed at %s", now.isoformat())
            purged = False
        return purged

    def _expire_releases(self, now: datetime) -> None:
        """Тайм-ауты релизов не чаще интервала."""
        if self._last_expiry is not None and now - self._last_expiry < self._expiry_interval:
            return
        self._last_expiry = now
        self._releases.expire(now)

    def _purge(self, now: datetime) -> bool:
        """Удаление истории старше срока не чаще интервала; срок 0 и меньше — хранить вечно."""
        if self._last_purge is not None and now - self._last_purge < self._purge_interval:
            return False
        self._last_purge = now
        if self._retention_days <= 0:
            return False
        cutoff = now - timedelta(days=self._retention_days)
        self._purger.purge(cutoff)
        logger.info("History older than %s purged", cutoff.isoformat())
        return True
