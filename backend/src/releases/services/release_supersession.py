"""Вытеснение: новый релиз сервиса/окружения завершает незавершенные предыдущие."""
from datetime import datetime

from releases.adapters.release_repository import ReleaseRepository
from releases.entity import SUPERSEDED, Release


class ReleaseSupersession:
    """Незавершенные старые релизы становятся superseded; запоздавшее событие старого деплоя — тоже."""

    def __init__(self, repository: ReleaseRepository) -> None:
        """Запоминает репозиторий релизов."""
        self._repository = repository

    def supersede(self, release: Release, now: datetime) -> None:
        """Применяет вытеснение вокруг только что созданного релиза."""
        open_releases = self._repository.open_for(release.service_id, release.environment)
        others = [other for other in open_releases if other.id != release.id]
        newer = [other for other in others if (other.started_at, other.id) > (release.started_at, release.id)]
        for other in others:
            if other in newer:
                continue
            other.superseded_by = release.id
            other.finish(SUPERSEDED, f"Начат релиз #{release.id}", now)
            self._repository.save(other)
        if newer and release.is_open:
            # Запоздавшее событие старого деплоя: более новый релиз его уже заменил.
            replacement = newer[-1]
            release.superseded_by = replacement.id
            release.finish(SUPERSEDED, f"Начат релиз #{replacement.id}", now)
            self._repository.save(release)
