"""Релиз по отчету Bamboo: поиск или создание релиза и перенос в него сведений отчета."""
from datetime import datetime, timedelta

from releases.adapters.release_repository import ReleaseRepository
from releases.entity import STARTED, UNKNOWN, DeploymentReport, Release

KEY_SEPARATOR = "|"
TARGET_FIELDS = ("release", "version", "build_id", "build_url", "commit", "image", "image_digest")


class ReleaseRegistry:
    """Сопоставляет события Bamboo релизам: started/succeeded/failed одного запуска — один релиз."""

    def __init__(self, repository: ReleaseRepository, lead: timedelta, timeout: timedelta) -> None:
        """Запоминает репозиторий, запас окна до начала (расхождение часов) и тайм-аут раскатки."""
        self._repository = repository
        self._lead = lead
        self._timeout = timeout

    def apply(self, report: DeploymentReport, received_at: datetime) -> tuple[Release, bool]:
        """Релиз отчета и признак, что он создан этим отчетом."""
        occurred = report.occurred_at or received_at
        key = report.field("deployment_id") or f"{report.build_id}{KEY_SEPARATOR}{report.field('release')}"
        latest = self._repository.latest_by_key(report.service_id, report.environment, key)
        created = not self._reusable(latest, report)
        if created:
            window = self._lead if report.status == STARTED else self._timeout
            release = Release(service_id=report.service_id, environment=report.environment, key=key,
                              started_at=occurred, window_start=occurred - window)
        else:
            release = latest
        self._apply_status(release, report.status, occurred, created)
        _apply_target(release, report)
        if created:
            self._repository.add(release)
        else:
            self._repository.save(release)
        result = (release, created)
        return result

    def _reusable(self, latest: Release | None, report: DeploymentReport) -> bool:
        """Без ID деплоя новый started после завершенного запуска той же сборки — это передеплой."""
        if latest is None:
            return False
        reusable = bool(report.field("deployment_id")) or latest.bamboo_status in ("", STARTED) \
            or report.status != STARTED
        return reusable

    def _apply_status(self, release: Release, status: str, occurred: datetime, created: bool) -> None:
        """started сдвигает начало релиза и его окна; итоговый статус Bamboo запоминается с временем."""
        if status != STARTED:
            release.bamboo_status = status
            release.bamboo_finished_at = occurred
            return
        if created or release.bamboo_status != STARTED:
            release.started_at = occurred
            release.window_start = min(release.window_start, occurred - self._lead)
        release.bamboo_status = release.bamboo_status or STARTED


def _apply_target(release: Release, report: DeploymentReport) -> None:
    """Заполняет пустые поля цели и инициатора из отчета."""
    for name in TARGET_FIELDS:
        if report.field(name) and not getattr(release, name):
            setattr(release, name, report.field(name))
    if not release.version:
        release.version = release.release
    actor = report.request.get("actor", UNKNOWN)
    if actor != UNKNOWN and release.initiator == UNKNOWN:
        release.initiator = actor
        release.initiator_kind = report.request.get("actor_kind", UNKNOWN)
