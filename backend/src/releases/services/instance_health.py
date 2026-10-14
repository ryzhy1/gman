"""Состояние экземпляра раскатки: здоров, завершился успешно, падает, создан после начала релиза."""
from releases.entity import Release, RolloutInstance
from releases.services.timestamp_parser import parse_timestamp

RUNNING = "running"
HEALTHY_VALUES = ("", "healthy")
COMPLETED_STATUSES = ("exited", "terminated")
SUCCESS_EXIT_CODE = 0
CRASH_REASONS = ("CrashLoopBackOff", "OOMKilled")
REASON_SEPARATOR = " "


class InstanceHealth:
    """Оценки состояния экземпляра для статуса релиза."""

    def __init__(self, crash_restarts: int) -> None:
        """Запоминает, со скольких перезапусков новый экземпляр считается падающим."""
        self._crash_restarts = crash_restarts

    def healthy(self, instance: RolloutInstance) -> bool:
        """Работает, проверки здоровья и готовности не провалены, причины сбоя нет."""
        healthy = (instance.status == RUNNING and instance.health in HEALTHY_VALUES and instance.ready is not False
                   and not instance.reason)
        return healthy

    def completed(self, instance: RolloutInstance) -> bool:
        """Завершился с кодом 0 (задание, а не сервис)."""
        completed = instance.status in COMPLETED_STATUSES and instance.exit_code == SUCCESS_EXIT_CODE
        return completed

    def ok(self, instance: RolloutInstance) -> bool:
        """Здоров или успешно завершился."""
        ok = self.healthy(instance) or self.completed(instance)
        return ok

    def crash(self, instance: RolloutInstance) -> str | None:
        """Признак падения: число перезапусков или CrashLoopBackOff/OOMKilled; иначе None."""
        if instance.restarts >= self._crash_restarts:
            reason = f"{instance.restarts} перезапусков"
            return reason
        text = REASON_SEPARATOR.join((instance.reason, instance.last_termination_reason))
        for crash_reason in CRASH_REASONS:
            if crash_reason in text:
                return crash_reason
        return None

    def fresh(self, instance: RolloutInstance, release: Release) -> bool:
        """Создан (или запущен) после начала окна релиза."""
        created = parse_timestamp(instance.created_at) or parse_timestamp(instance.started_at)
        fresh = created is not None and created >= release.window_start
        return fresh
