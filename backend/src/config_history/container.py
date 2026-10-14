"""Сборка модуля config_history: репозиторий версий, сервисы и оркестратор."""
from config_history.adapters.config_version_repository import ConfigVersionRepository
from config_history.orchestrator import ConfigHistoryDependencies, ConfigHistoryOrchestrator
from config_history.services.config_diff import ConfigDiff
from config_history.services.version_catalog import VersionCatalog
from database.unit_of_work import UnitOfWork


def build_config_history_orchestrator(unit_of_work: UnitOfWork) -> ConfigHistoryOrchestrator:
    """Создает адаптер, сервисы и оркестратор поверх общей границы транзакций."""
    repository = ConfigVersionRepository(unit_of_work)
    config_diff = ConfigDiff()
    version_catalog = VersionCatalog(repository, config_diff)
    dependencies = ConfigHistoryDependencies(transaction=unit_of_work.transaction, version_catalog=version_catalog)
    orchestrator = ConfigHistoryOrchestrator(dependencies)
    return orchestrator
