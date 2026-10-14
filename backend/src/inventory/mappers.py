"""Маппинг модуля inventory: проверенные пейлоады API -> DTO, DTO -> ответы API."""
from dataclasses import asdict
from datetime import datetime
from typing import Any

from inventory.entity import (CatalogService, DuplicateInstanceError, HistoryPage, JsonObject, LogTarget,
                              ObservationContext, ObservedInstance, RuntimeEventReport, ServiceIdMismatchError,
                              SnapshotReport, SourceView, SplunkLinks, SplunkOverride)

# Не сообщаются агентами до 0.8: опускаются, а не хранятся как null, чтобы хеш конфигурации их экземпляров
# не изменился и одно лишь обновление API не создавало новых версий.
OPTIONAL_CONFIG = ("command", "args")
SOURCE_ONLY_EXCLUDED = ("instances", "events", "received_at")
CORE_SERVICE_FIELDS = ("id", "name", "description", "owner", "team", "repository", "grafana", "splunk", "bamboo")
INSTANCE_KEY_SEPARATOR = "/"


def snapshot_from_payload(payload: JsonObject) -> SnapshotReport:
    """Снимок из проверенного пейлоада (JSON-режим); повтор ID экземпляров — ошибка."""
    instances = []
    for document in payload["instances"]:
        _drop_unreported_config(document)
        instance = ObservedInstance(document=document)
        instances.append(instance)
    if len({instance.id for instance in instances}) != len(instances):
        raise DuplicateInstanceError("Duplicate instance IDs")
    events = []
    for document in payload["events"]:
        event = RuntimeEventReport(document=document, last_at=datetime.fromisoformat(document["last_at"]))
        events.append(event)
    source_document = {key: value for key, value in payload.items() if key not in SOURCE_ONLY_EXCLUDED}
    report = SnapshotReport(source_id=payload["source_id"], source_type=payload["source_type"],
                            hostname=payload["hostname"], observed_at=datetime.fromisoformat(payload["observed_at"]),
                            observed_at_text=payload["observed_at"], errors=list(payload["errors"]),
                            instances=instances, events=events, source_document=source_document)
    return report


def observation_context(report: SnapshotReport, received_at: datetime) -> ObservationContext:
    """Общие поля событий истории снимка."""
    context = ObservationContext(source_id=report.source_id, observed_at=report.observed_at,
                                 observed_at_text=report.observed_at_text, received_at=received_at)
    return context


def _drop_unreported_config(document: JsonObject) -> None:
    """Убирает необязательные поля конфигурации, которых агент не сообщил."""
    config = document["config"]
    for key in OPTIONAL_CONFIG:
        if config.get(key, "") is None:
            del config[key]


def catalog_service_from_payload(path_service_id: str, payload: JsonObject) -> CatalogService:
    """Сервис каталога из тела запроса; ID в пути и в теле должны совпадать."""
    if path_service_id != payload["id"]:
        raise ServiceIdMismatchError("ID mismatch")
    core = {name: payload[name] for name in CORE_SERVICE_FIELDS if name in payload}
    extra = {key: value for key, value in payload.items() if key not in CORE_SERVICE_FIELDS}
    service = CatalogService(**core, extra=extra)
    return service


def catalog_service_to_response(service: CatalogService) -> JsonObject:
    """Сервис в формате API: основные поля и необязательные разделы."""
    response = {name: getattr(service, name) for name in CORE_SERVICE_FIELDS}
    response.update(service.extra)
    return response


def splunk_override_from_service(service: CatalogService) -> SplunkOverride:
    """Переопределение логов из раздела logs.splunk карточки сервиса."""
    logs = service.extra.get("logs") or {}
    splunk = logs.get("splunk") or {}
    override = SplunkOverride(index=splunk.get("index", ""), query=splunk.get("query", ""))
    return override


def log_targets_from_sources(documents: list[JsonObject]) -> list[LogTarget]:
    """Экземпляры всех источников как цели ссылок на логи; хост — узел pod-а или хост источника."""
    targets = []
    for source in documents:
        for item in source.get("instances", []):
            target = LogTarget(key=source["source_id"] + INSTANCE_KEY_SEPARATOR + item["id"],
                               service_id=item.get("service_id", ""), instance_id=item["id"],
                               name=item.get("name", ""), runtime=item.get("runtime", ""),
                               namespace=item.get("namespace", ""), environment=item.get("environment", ""),
                               host=item.get("node") or source.get("hostname", ""))
            targets.append(target)
    return targets


def splunk_links_to_response(links: SplunkLinks) -> JsonObject:
    """Ссылки Splunk в формате API."""
    response = {"enabled": links.enabled, "error": links.error, "service": links.service,
                "instances": links.instances}
    return response


def history_page_to_response(page: HistoryPage) -> JsonObject:
    """Страница аудита в формате API."""
    response = {"events": page.events, "next": page.next_cursor}
    return response


def overview_to_response(services: list[CatalogService], sources: list[SourceView], events: HistoryPage,
                         settings: dict[str, Any], server_time: datetime) -> JsonObject:
    """Обзор портала: каталог, источники, последние события, адрес Grafana и темп обновления."""
    response = {"services": [catalog_service_to_response(service) for service in services],
                "sources": [_source_to_response(view) for view in sources],
                "server_time": server_time.isoformat(),
                # Адрес Grafana для браузера — для ссылок на дашборды по умолчанию.
                "grafana_url": settings["grafana_url"],
                # Секунды между точками метрик; портал обновляется в том же темпе.
                "refresh_seconds": settings["refresh_seconds"],
                "events": events.events}
    return response


def _source_to_response(view: SourceView) -> JsonObject:
    """Источник в формате API; у кластера — сводка Events узлов."""
    if view.node_events is None:
        return view.document
    node_events = []
    for summary in view.node_events:
        document = {**asdict(summary), "last_at": summary.last_at.isoformat()}
        node_events.append(document)
    response = {**view.document, "node_events": node_events}
    return response


def ingest_response(accepted: bool) -> JsonObject:
    """Ответ на снимок: принят или отброшен как повтор/устаревший."""
    if accepted:
        return {"accepted": True}
    return {"accepted": False, "reason": "duplicate or older snapshot"}
