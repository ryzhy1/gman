"""Пары сервис/окружение, чьи экземпляры изменились в снимке: для них пересчитываются релизы."""
from inventory.entity import ObservedInstance

Scope = tuple[str, str]


class RolloutScope:
    """Определяет затронутые снимком пары сервис/окружение."""

    def touched(self, previous: dict[str, ObservedInstance], current: list[ObservedInstance]) -> set[Scope]:
        """Пары исчезнувших, появившихся и изменившихся экземпляров.

        Первый снимок тоже может завершить раскатку: эти экземпляры видны впервые.
        """
        current_by_id = {instance.id: instance for instance in current}
        scopes = set()
        for instance in previous.values():
            if current_by_id.get(instance.id) != instance:
                scopes.add((instance.service_id, instance.environment))
        for instance in current:
            if previous.get(instance.id) != instance:
                scopes.add((instance.service_id, instance.environment))
        return scopes
