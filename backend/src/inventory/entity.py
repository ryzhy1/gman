"""Сущности модуля inventory: каталог сервисов, снимки источников, экземпляры и события истории.

Экземпляр и Event Kubernetes хранятся и сравниваются как JSON-документ, присланный агентом (поля, добавленные
в API, проходят без миграции), поэтому сущности оборачивают документ и дают типизированный доступ к полям,
которые использует бизнес-логика.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

JsonObject = dict[str, Any]
UNKNOWN = "unknown"
RUNTIME_OBSERVATION = "runtime observation"
STARTED_BY_LABEL = "container label gman.started-by (not verified)"


class InventoryError(Exception):
    """Базовая ошибка модуля inventory."""


class DuplicateInstanceError(InventoryError):
    """В снимке повторяются ID экземпляров."""


class ServiceIdMismatchError(InventoryError):
    """ID сервиса в пути не совпадает с ID в теле запроса."""


class UnknownServiceError(InventoryError):
    """Сервиса нет в каталоге."""


class SplunkTemplateError(InventoryError):
    """Шаблон поиска Splunk или настройки ссылок некорректны."""


class InvalidCursorError(InventoryError):
    """Курсор страницы истории не удалось разобрать."""


@dataclass(frozen=True)
class CatalogService:
    """Сервис каталога; extra — необязательные разделы модели API без своей колонки (например, logs)."""

    id: str
    name: str
    description: str = ""
    owner: str = ""
    team: str = ""
    repository: str = ""
    grafana: str = ""
    splunk: str = ""
    bamboo: str = ""
    extra: JsonObject = field(default_factory=dict)


@dataclass
class ObservedInstance:
    """Экземпляр (контейнер Docker или контейнер pod-а) в виде документа агента."""

    document: JsonObject

    @property
    def id(self) -> str:
        """ID экземпляра в источнике."""
        instance_id = self.document["id"]
        return instance_id

    @property
    def name(self) -> str:
        """Имя экземпляра."""
        name = self.document["name"]
        return name

    @property
    def service_id(self) -> str:
        """Сервис каталога из метки gman.service (пусто — без сервиса)."""
        service_id = self.document.get("service_id", "")
        return service_id

    @property
    def environment(self) -> str:
        """Окружение из метки gman.environment."""
        environment = self.document.get("environment", UNKNOWN)
        return environment

    @property
    def config(self) -> JsonObject:
        """Конфигурация запуска (сохраняется версиями)."""
        config = self.document["config"]
        return config

    @property
    def started_by(self) -> str:
        """Кто создал экземпляр по метке gman.started-by (пусто — метки нет); значение не проверяется."""
        started_by = (self.document.get("origin") or {}).get("started_by", "")
        return started_by

    @property
    def last_oom_at(self) -> str | None:
        """Время последнего OOM в ISO-формате."""
        moment = self.document.get("last_oom_at")
        return moment


@dataclass(frozen=True)
class RuntimeEventReport:
    """Event Kubernetes pod-а или узла, присланный агентом (без свободного текста)."""

    document: JsonObject
    last_at: datetime

    @property
    def uid(self) -> str:
        """UID объекта Event."""
        uid = self.document["uid"]
        return uid

    @property
    def count(self) -> int:
        """Счетчик повторений по данным Kubernetes."""
        count = self.document["count"]
        return count

    @property
    def is_node_event(self) -> bool:
        """Event узла, а не pod-а."""
        node_event = self.document.get("kind") == "Node"
        return node_event


@dataclass
class SnapshotReport:
    """Снимок источника: свойства источника, экземпляры, ошибки сбора и Events с прошлого отчета."""

    source_id: str
    source_type: str
    hostname: str
    observed_at: datetime
    observed_at_text: str
    errors: list[str]
    instances: list[ObservedInstance]
    events: list[RuntimeEventReport]
    # Документ источника без экземпляров и Events: последнее состояние источника.
    source_document: JsonObject


@dataclass(frozen=True)
class SnapshotIntake:
    """Результат приема снимка: предыдущее время наблюдения и экземпляры предыдущего снимка."""

    previous_observed_at: datetime | None
    previous_instances: dict[str, ObservedInstance]

    @property
    def first(self) -> bool:
        """Первый снимок источника."""
        first = self.previous_observed_at is None
        return first


@dataclass(frozen=True)
class ObservationContext:
    """Общие поля событий истории одного снимка."""

    source_id: str
    observed_at: datetime
    observed_at_text: str
    received_at: datetime

    def base_fields(self) -> JsonObject:
        """Поля, общие для всех событий снимка; автор изменений runtime не известен."""
        fields = {"source_id": self.source_id, "occurred_at": self.observed_at_text,
                  "received_at": self.received_at.isoformat(), "actor": UNKNOWN,
                  "actor_source": RUNTIME_OBSERVATION}
        return fields


@dataclass(frozen=True)
class HistoryEvent:
    """Событие истории (аудита) до записи."""

    data: JsonObject
    received_at: datetime
    occurred_at: datetime | None


@dataclass
class EventMark:
    """Последние учтенные счетчик и время Event Kubernetes."""

    source_id: str
    uid: str
    count: int
    last_at: datetime
    received_at: datetime
    node: str = ""
    reason: str = ""


@dataclass(frozen=True)
class NodeEventSummary:
    """Events узла с одной причиной за окно: сумма счетчиков, число Events и последнее время."""

    node: str
    reason: str
    count: int
    events: int
    last_at: datetime


@dataclass(frozen=True)
class SourceView:
    """Источник для обзора: документ последнего снимка с экземплярами; у кластера — сводка Events узлов."""

    document: JsonObject
    node_events: list[NodeEventSummary] | None = None


@dataclass(frozen=True)
class HistoryPage:
    """Страница истории (новые первыми) и курсор следующей."""

    events: list[JsonObject]
    next_cursor: str | None


@dataclass(frozen=True)
class SplunkSettings:
    """Ссылки на поиск Splunk: страница поиска, шаблон SPL и индексы по окружениям ("*" — запасной)."""

    url: str
    search: str
    indexes: dict[str, str]


@dataclass(frozen=True)
class SplunkOverride:
    """Переопределение индекса и шаблона в карточке сервиса."""

    index: str | dict[str, str] = ""
    query: str = ""


@dataclass(frozen=True)
class LogTarget:
    """Экземпляр, для которого строится ссылка на логи."""

    key: str
    service_id: str
    instance_id: str
    name: str
    runtime: str
    namespace: str
    environment: str
    host: str


@dataclass(frozen=True)
class SplunkLinks:
    """Ссылка сервиса и ссылки его экземпляров ("<source_id>/<instance_id>")."""

    enabled: bool
    service: str | None
    instances: dict[str, str]
    error: str | None = None
