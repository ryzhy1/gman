"""Версии конфигураций: страницы по сервису или экземпляру, отдельная версия и сравнение двух версий."""
from config_history.adapters.config_version_repository import ConfigVersionRepository
from config_history.entity import (ConfigComparison, ConfigVersion, UnknownInstanceError, UnknownServiceError,
                                   UnknownVersionError, VersionPage, VersionScope)
from config_history.services.config_diff import ConfigDiff


class VersionCatalog:
    """Доменные правила чтения версий: 404 для неизвестных объектов, сравнение любых двух версий."""

    def __init__(self, repository: ConfigVersionRepository, config_diff: ConfigDiff) -> None:
        """Запоминает репозиторий версий и сервис сравнения."""
        self._repository = repository
        self._diff = config_diff

    def service_page(self, service_id: str, limit: int, before: str) -> VersionPage:
        """Версии всех экземпляров сервиса; сервис каталога без версий — пустая страница, не ошибка."""
        if not self._repository.service_known(service_id):
            raise UnknownServiceError(service_id)
        scope = VersionScope(service_id=service_id)
        page = self._repository.page(scope, limit, before)
        return page

    def instance_page(self, source_id: str, instance_id: str, limit: int, before: str) -> VersionPage:
        """Версии одного экземпляра, в том числе исчезнувшего."""
        if not self._repository.instance_known(source_id, instance_id):
            raise UnknownInstanceError(f"{source_id}/{instance_id}")
        scope = VersionScope(source_id=source_id, instance_id=instance_id)
        page = self._repository.page(scope, limit, before)
        return page

    def version(self, version_id: int) -> ConfigVersion:
        """Версия с телом конфигурации."""
        version = self._repository.get(version_id)
        if version is None:
            raise UnknownVersionError(version_id)
        return version

    def compare(self, older_id: int, newer_id: int) -> ConfigComparison:
        """Изменения от версии `older_id` к версии `newer_id`; сравнивать можно любые две версии."""
        older = self.version(older_id)
        newer = self.version(newer_id)
        changes = self._diff.diff(older.config, newer.config)
        summary = self._diff.summary(changes)
        comparison = ConfigComparison(older=older, newer=newer, changes=changes, summary=summary)
        return comparison
