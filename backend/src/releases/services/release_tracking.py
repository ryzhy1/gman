"""Учет релизов по наблюдениям runtime: связывание событий и пересчет статусов под блокировкой пары."""
from datetime import datetime, timedelta

from database.advisory_lock import AdvisoryLock
from releases.adapters.release_repository import ReleaseRepository
from releases.adapters.rollout_instance_repository import RolloutInstanceRepository
from releases.adapters.runtime_event_repository import RuntimeEventRepository
from releases.entity import Release
from releases.services.event_correlator import EventCorrelator
from releases.services.release_evaluator import ReleaseEvaluator

LOCK_PREFIX = "release:"
SCOPE_SEPARATOR = "|"


class ReleaseTracking:
    """Пересчет релизов сервиса/окружения; оценка идемпотентна, повторы снимков отсекаются раньше."""

    def __init__(self, releases: ReleaseRepository, instances: RolloutInstanceRepository,
                 events: RuntimeEventRepository, lock: AdvisoryLock, correlator: EventCorrelator,
                 evaluator: ReleaseEvaluator, timeout: timedelta) -> None:
        """Запоминает репозитории, блокировку, связывание событий, оценку статуса и тайм-аут."""
        self._releases = releases
        self._instances = instances
        self._events = events
        self._lock = lock
        self._correlator = correlator
        self._evaluator = evaluator
        self._timeout = timeout

    def lock_scope(self, service_id: str, environment: str) -> None:
        """Сериализует пересчет релизов одной пары сервис/окружение."""
        self._lock.acquire(f"{LOCK_PREFIX}{service_id}{SCOPE_SEPARATOR}{environment}")

    def track_scope(self, service_id: str, environment: str, event_ids: list[int], now: datetime) -> None:
        """После снимка: связывает его события с открытыми релизами пары и пересчитывает их."""
        if not service_id:
            return
        self.lock_scope(service_id, environment)
        releases = self._releases.open_for(service_id, environment)
        if not releases:
            return
        instances = self._instances.current(service_id, environment)
        events = self._events.by_ids(event_ids, service_id)
        # Новые первыми: более новый релиз забирает события пересекающихся окон.
        for release in releases:
            linked = self._correlator.correlate(release, events, instances)
            self._events.save_links(linked)
        for release in releases:
            self._evaluator.evaluate(release, instances, now)
            self._releases.save(release)

    def correlate_window(self, release: Release) -> None:
        """Открытый релиз забирает уже записанные несвязанные изменения runtime своего окна."""
        if not release.is_open:
            return
        events = self._events.uncorrelated(release.service_id, release.window_start,
                                           self._correlator.window_end(release))
        instances = self._instances.current(release.service_id, release.environment)
        linked = self._correlator.correlate(release, events, instances)
        self._events.save_links(linked)

    def evaluate(self, release: Release, now: datetime) -> None:
        """Пересчитывает статус релиза по экземплярам, наблюдаемым сейчас."""
        instances = self._instances.current(release.service_id, release.environment)
        self._evaluator.evaluate(release, instances, now)
        self._releases.save(release)

    def expire(self, now: datetime) -> None:
        """Пересчитывает открытые релизы старше тайм-аута: раскатка, которая не сошлась, завершается."""
        for service_id, environment in sorted(self._releases.stale_scopes(now - self._timeout)):
            self.lock_scope(service_id, environment)
            for release in self._releases.open_for(service_id, environment):
                self.evaluate(release, now)
