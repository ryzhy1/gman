"""Маппинг модуля config_history: DTO -> ответы API."""
from dataclasses import asdict
from typing import Any

from config_history.entity import ConfigComparison, ConfigVersion, VersionPage


def version_to_response(version: ConfigVersion) -> dict[str, Any]:
    """Версия в формате API; тело конфигурации — только если оно прочитано."""
    response = {"id": version.id, "service_id": version.service_id, "source_id": version.source_id,
                "instance_id": version.instance_id, "instance_name": version.instance_name,
                "environment": version.environment, "config_hash": version.config_hash,
                "first_seen_at": version.first_seen_at.isoformat(), "last_seen_at": version.last_seen_at.isoformat(),
                "current": version.current}
    if version.config is not None:
        response["config"] = version.config
    return response


def summary_to_response(version: ConfigVersion) -> dict[str, Any]:
    """Версия без тела конфигурации (для сравнения и списков)."""
    response = version_to_response(version)
    response.pop("config", None)
    return response


def page_to_response(page: VersionPage) -> dict[str, Any]:
    """Страница версий без тел конфигураций."""
    versions = [summary_to_response(version) for version in page.versions]
    response = {"versions": versions, "next": page.next_cursor}
    return response


def comparison_to_response(comparison: ConfigComparison) -> dict[str, Any]:
    """Сравнение версий: обе версии без тел, признак идентичности, сводка и изменения."""
    changes = [asdict(change) for change in comparison.changes]
    response = {"from": summary_to_response(comparison.older), "to": summary_to_response(comparison.newer),
                "identical": comparison.identical, "summary": comparison.summary, "changes": changes}
    return response
