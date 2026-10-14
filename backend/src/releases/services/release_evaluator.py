"""Статус релиза по отчету Bamboo и экземплярам, наблюдаемым сейчас."""
from datetime import datetime, timedelta

from releases.entity import FAILED, ROLLING_OUT, STARTED, SUCCEEDED, SUPERSEDED, Release, RolloutInstance
from releases.services.image_matcher import BY_DIGEST, BY_VERSION, ImageMatcher, image_repository
from releases.services.instance_health import InstanceHealth

BROKEN_SHOWN = 5
SECONDS_PER_MINUTE = 60


class ReleaseEvaluator:
    """Продвигает статус релиза; повторная оценка тех же данных ничего не меняет."""

    def __init__(self, matcher: ImageMatcher, health: InstanceHealth, timeout: timedelta) -> None:
        """Запоминает сопоставление образов, оценку экземпляров и тайм-аут раскатки."""
        self._matcher = matcher
        self._health = health
        self._timeout = timeout

    def evaluate(self, release: Release, instances: list[RolloutInstance], now: datetime) -> None:
        """Новый статус релиза: ошибка Bamboo, сбой новых экземпляров, успех или тайм-аут."""
        if release.status == SUPERSEDED:
            return
        if release.bamboo_status == FAILED:
            if release.status != FAILED:
                release.finish(FAILED, "Bamboo сообщил об ошибке деплоя", release.bamboo_finished_at or now)
            return
        if not release.is_open:
            return
        self._advance(release, instances, now)

    def _advance(self, release: Release, instances: list[RolloutInstance], now: datetime) -> None:
        """Прогресс раскатки и переход к rolling-out, failed или succeeded."""
        matched = self._matched(release, instances)
        repositories = self._matcher.repositories(release, instances)
        relevant = [instance for instance in instances if image_repository(instance.image) in repositories]
        ok = [instance for instance in matched if self._health.ok(instance)]
        new = [instance for instance in matched if self._health.fresh(instance, release)]
        progress = {"total": len(relevant), "on_target": len(matched), "healthy": len(ok), "fresh": len(new)}
        if release.progress != progress:
            release.progress = progress
        if matched:
            self._mark_rolling_out(release, instances, now)
        broken = self._broken(new)
        if broken:
            release.finish(FAILED, "Сбой новых экземпляров: " + ", ".join(broken[:BROKEN_SHOWN]), now)
            return
        converged = relevant and len(matched) == len(relevant) and len(ok) == len(matched)
        if converged and (new or release.bamboo_status == SUCCEEDED):
            release.finish(SUCCEEDED, "", now)
            return
        if now - release.started_at > self._timeout:
            release.finish(FAILED, self._timeout_reason(instances, matched, relevant, ok), now)

    def _matched(self, release: Release, instances: list[RolloutInstance]) -> list[RolloutInstance]:
        """Экземпляры на целевой версии."""
        matched = [instance for instance in instances if self._matcher.match(instance, release)]
        return matched

    def _mark_rolling_out(self, release: Release, instances: list[RolloutInstance], now: datetime) -> None:
        """Способ сопоставления (digest важнее версии) и начало раскатки."""
        methods = {self._matcher.match(instance, release) for instance in instances}
        correlation = BY_DIGEST if BY_DIGEST in methods else BY_VERSION
        if release.correlation != correlation:
            release.correlation = correlation
        if release.status == STARTED:
            release.status = ROLLING_OUT
            release.rollout_started_at = now

    def _broken(self, new: list[RolloutInstance]) -> list[str]:
        """Падающие новые экземпляры как "имя (причина)"."""
        broken = []
        for instance in new:
            crash = self._health.crash(instance)
            if crash:
                broken.append(f"{instance.name} ({crash})")
        return broken

    def _timeout_reason(self, instances: list[RolloutInstance], matched: list[RolloutInstance],
                        relevant: list[RolloutInstance], ok: list[RolloutInstance]) -> str:
        """Причина тайм-аута: чего не хватило раскатке."""
        if not instances:
            reason = "экземпляры сервиса в этом окружении не наблюдаются"
        elif not matched:
            reason = "ни один экземпляр не запущен с целевым образом"
        else:
            reason = f"на целевой версии {len(matched)} из {len(relevant)}, здоровы {len(ok)}"
        minutes = int(self._timeout.total_seconds() // SECONDS_PER_MINUTE)
        message = f"Тайм-аут {minutes} мин: {reason}"
        return message
