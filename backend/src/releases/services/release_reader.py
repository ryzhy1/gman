"""Чтение релизов: страницы и карточка релиза с событиями и текущими экземплярами раскатки."""
from releases.adapters.release_repository import ReleaseRepository
from releases.adapters.rollout_instance_repository import RolloutInstanceRepository
from releases.adapters.runtime_event_repository import RuntimeEventRepository
from releases.entity import InstanceVerdict, ReleaseDetail, ReleaseFilter, ReleaseNotFoundError, ReleasePage
from releases.services.image_matcher import ImageMatcher, image_repository
from releases.services.instance_health import InstanceHealth

DETAIL_EVENTS = 500


class ReleaseReader:
    """Релизы для портала."""

    def __init__(self, releases: ReleaseRepository, instances: RolloutInstanceRepository,
                 events: RuntimeEventRepository, matcher: ImageMatcher, health: InstanceHealth) -> None:
        """Запоминает репозитории, сопоставление образов и оценку экземпляров."""
        self._releases = releases
        self._instances = instances
        self._events = events
        self._matcher = matcher
        self._health = health

    def page(self, release_filter: ReleaseFilter) -> ReleasePage:
        """Новые первыми по началу; курсор `next` — для следующей страницы."""
        page = self._releases.page(release_filter)
        return page

    def detail(self, release_id: int) -> ReleaseDetail:
        """Релиз, связанные события и экземпляры раскатки (без sidecar-ов и других образов сервиса)."""
        release = self._releases.get(release_id)
        if release is None:
            raise ReleaseNotFoundError(str(release_id))
        events = self._events.linked_to(release_id, DETAIL_EVENTS)
        instances = self._instances.current(release.service_id, release.environment)
        repositories = self._matcher.repositories(release, instances)
        verdicts = []
        for instance in instances:
            method = self._matcher.match(instance, release)
            if not method and image_repository(instance.image) not in repositories:
                continue
            verdict = InstanceVerdict(instance=instance, on_target=method, healthy=self._health.ok(instance),
                                      new=self._health.fresh(instance, release),
                                      crash=self._health.crash(instance) if method else None)
            verdicts.append(verdict)
        detail = ReleaseDetail(release=release, events=events, instances=verdicts)
        return detail
