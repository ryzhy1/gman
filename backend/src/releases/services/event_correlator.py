"""Связывание событий runtime из окна релиза с релизом и атрибуция изменений инициатору деплоя."""
from datetime import datetime, timedelta

from releases.entity import RUNTIME_TYPES, Release, RolloutInstance, RuntimeEvent
from releases.services.image_matcher import ImageMatcher, image_repository

INSTANCE_REMOVED = "instance-removed"
# Изменения, которые вызывает деплой; смены состояния и перезапуски связываются, но не приписываются.
ATTRIBUTED = ("instance-appeared", "config-changed")


class EventCorrelator:
    """Решает, какие события относятся к релизу."""

    def __init__(self, matcher: ImageMatcher, timeout: timedelta) -> None:
        """Запоминает сопоставление образов и тайм-аут раскатки (конец окна незавершенного релиза)."""
        self._matcher = matcher
        self._timeout = timeout

    def window_end(self, release: Release) -> datetime:
        """Конец окна релиза: завершение или начало плюс тайм-аут."""
        end = release.finished_at or (release.started_at + self._timeout)
        return end

    def correlate(self, release: Release, events: list[RuntimeEvent],
                  instances: list[RolloutInstance]) -> list[RuntimeEvent]:
        """Связывает события окна релиза; возвращает связанные (их данные изменены)."""
        observed = instances + [event.after for event in events if event.after]
        repositories = self._matcher.repositories(release, observed)
        linked = []
        for event in events:
            if not self._in_window(event, release):
                continue
            if event.type == INSTANCE_REMOVED:
                # Уход старой версии — часть раскатки, но ничто не доказывает, кто ее удалил.
                if self._old_version_removed(event.before, release, repositories):
                    _link(event, release, None)
                    linked.append(event)
                continue
            method = self._matcher.match(event.after, release)
            if method:
                _link(event, release, method)
                linked.append(event)
        return linked

    def _in_window(self, event: RuntimeEvent, release: Release) -> bool:
        """Несвязанное событие runtime в окне релиза и в его окружении."""
        if event.release_id is not None or event.type not in RUNTIME_TYPES or event.occurred_at is None:
            return False
        if not release.window_start <= event.occurred_at <= self.window_end(release):
            return False
        observed = event.after or event.before
        in_window = observed is not None and observed.environment == release.environment
        return in_window

    def _old_version_removed(self, before: RolloutInstance | None, release: Release,
                             repositories: set[str]) -> bool:
        """Удален экземпляр образа раскатки, работавший не на целевой версии."""
        if before is None:
            return False
        removed = image_repository(before.image) in repositories and not self._matcher.match(before, release)
        return removed


def _link(event: RuntimeEvent, release: Release, method: str | None) -> None:
    """Связывает событие с релизом; изменение, вызванное деплоем, приписывает инициатору."""
    data = {**event.data, "release_id": release.id}
    if method and event.type in ATTRIBUTED:
        data.update(actor=release.initiator, actor_kind=release.initiator_kind,
                    actor_source=f"Bamboo, correlated by {method}")
    event.data = data
    event.release_id = release.id
