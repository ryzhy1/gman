"""Сборка модуля exposition: репозиторий состояния, наблюдение за TLS-файлами, сервисы и оркестратор."""
from dataclasses import dataclass

from config.settings import Settings
from database.unit_of_work import UnitOfWork
from exposition.adapters.inventory_state_repository import InventoryStateRepository
from exposition.adapters.tls_file_reader import TlsFileReader
from exposition.orchestrator import ExpositionDependencies, ExpositionOrchestrator
from exposition.services.inventory_samples import InventorySamples
from exposition.services.inventory_state import InventoryState
from exposition.services.text_format import TextFormat
from exposition.services.tls_parser import TlsParser
from exposition.services.tls_samples import TlsSamples
from exposition.services.tls_watch import TlsWatch

# Тот же порог, что у GManSourceSilent: более старые показания GPU не выводятся как текущие.
GPU_FRESH_SECONDS = 300.0
TLS_FILE_MAX_BYTES = 1 << 20
PATH_LIST_SEPARATOR = ","


@dataclass(frozen=True)
class ExpositionConfig:
    """Узкая конфигурация модуля: наблюдаемые TLS-файлы и окно свежести GPU."""

    tls_watch_files: list[str]
    gpu_fresh_seconds: float


def exposition_config(settings: Settings) -> ExpositionConfig:
    """Выделяет из общих настроек список TLS-файлов (пустые элементы отбрасываются)."""
    paths = [path.strip() for path in settings.tls_watch_files.split(PATH_LIST_SEPARATOR) if path.strip()]
    config = ExpositionConfig(tls_watch_files=paths, gpu_fresh_seconds=GPU_FRESH_SECONDS)
    return config


def build_exposition_orchestrator(settings: Settings, unit_of_work: UnitOfWork) -> ExpositionOrchestrator:
    """Создает адаптеры, сервисы и оркестратор."""
    config = exposition_config(settings)
    repository = InventoryStateRepository(unit_of_work)
    tls_reader = TlsFileReader(TLS_FILE_MAX_BYTES)
    tls_parser = TlsParser()
    tls_watch = TlsWatch(config.tls_watch_files, tls_reader, tls_parser)
    inventory_state = InventoryState(repository)
    inventory_samples = InventorySamples(config.gpu_fresh_seconds)
    tls_samples = TlsSamples(tls_watch)
    text_format = TextFormat()
    dependencies = ExpositionDependencies(transaction=unit_of_work.transaction, inventory_state=inventory_state,
                                          inventory_samples=inventory_samples, tls_samples=tls_samples,
                                          text_format=text_format)
    orchestrator = ExpositionOrchestrator(dependencies)
    return orchestrator
