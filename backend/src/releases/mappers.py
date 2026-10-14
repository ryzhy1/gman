"""Маппинг модуля releases: пейлоад события CI -> DTO, релизы -> ответы API."""
from datetime import datetime
from typing import Any

from releases.entity import (UNKNOWN, DeploymentReport, InstanceVerdict, JsonObject, Release, ReleaseDetail,
                             ReleaseFilter, ReleasePage)

# Необязательные поля события CI со значениями по умолчанию: запросы, сохраненные до появления поля,
# сравниваются с повтором так, как будто поле было передано по умолчанию.
DEPLOYMENT_DEFAULTS: JsonObject = {"occurred_at": None, "actor": UNKNOWN, "actor_kind": UNKNOWN, "commit": "",
                                   "image_digest": "", "image": "", "version": "", "release": "",
                                   "deployment_id": "", "build_url": ""}
TIMESTAMP_FIELDS = ("started_at", "rollout_started_at", "bamboo_finished_at", "finished_at")
UTC_SUFFIX = "Z"
UTC_OFFSET = "+00:00"


def deployment_from_payload(payload: JsonObject) -> DeploymentReport:
    """Событие CI из проверенного (или ранее сохраненного) запроса с подставленными умолчаниями."""
    request = {**DEPLOYMENT_DEFAULTS, **payload}
    occurred_text = request["occurred_at"]
    occurred_at = datetime.fromisoformat(occurred_text.replace(UTC_SUFFIX, UTC_OFFSET)) if occurred_text else None
    report = DeploymentReport(event_id=request["event_id"], service_id=request["service_id"],
                              environment=request["environment"], status=request["status"],
                              build_id=request["build_id"], occurred_at=occurred_at, request=request)
    return report


def release_filter_from_params(params: dict[str, Any]) -> ReleaseFilter:
    """Отбор страницы релизов из параметров запроса."""
    release_filter = ReleaseFilter(limit=params["limit"], service_id=params.get("service_id"),
                                   environment=params.get("environment"), status=params.get("status"),
                                   before=params.get("before") or "")
    return release_filter


def deployment_response(release_id: int | None) -> JsonObject:
    """Ответ на событие CI: принято (с ID релиза) или повтор."""
    if release_id is None:
        return {"accepted": False, "reason": "duplicate"}
    response = {"accepted": True, "release_id": release_id}
    return response


def release_to_response(release: Release, now: datetime | None) -> JsonObject:
    """Релиз в формате API; длительность — до завершения или до текущего момента."""
    end = release.finished_at or now
    response = {
        "id": release.id, "service_id": release.service_id, "environment": release.environment,
        "release": release.release, "version": release.version, "build_id": release.build_id,
        "build_url": release.build_url, "commit": release.commit, "image": release.image,
        "image_digest": release.image_digest, "initiator": release.initiator,
        "initiator_kind": release.initiator_kind, "status": release.status,
        "status_reason": release.status_reason, "bamboo_status": release.bamboo_status,
        "correlation": release.correlation, "superseded_by": release.superseded_by,
        "progress": release.progress or {},
        "duration_seconds": (end - release.started_at).total_seconds() if end else None,
    }
    for name in TIMESTAMP_FIELDS:
        value = getattr(release, name)
        response[name] = value.isoformat() if value else None
    return response


def page_to_response(page: ReleasePage, now: datetime) -> JsonObject:
    """Страница релизов в формате API."""
    releases = [release_to_response(release, now) for release in page.releases]
    response = {"releases": releases, "next": page.next_cursor}
    return response


def detail_to_response(detail: ReleaseDetail, now: datetime) -> JsonObject:
    """Карточка релиза: релиз, события и экземпляры раскатки."""
    instances = [_verdict_to_response(verdict) for verdict in detail.instances]
    response = {"release": release_to_response(detail.release, now), "events": detail.events,
                "instances": instances}
    return response


def _verdict_to_response(verdict: InstanceVerdict) -> JsonObject:
    """Экземпляр раскатки с оценкой в формате API."""
    instance = verdict.instance
    response = {"source_id": instance.source_id, "id": instance.id, "name": instance.name, "node": instance.node,
                "status": instance.status, "health": instance.health, "ready": instance.ready,
                "reason": instance.reason, "restarts": instance.restarts, "image": instance.image,
                "image_id": instance.image_id, "created_at": instance.created_at,
                "started_at": instance.started_at, "on_target": verdict.on_target, "healthy": verdict.healthy,
                "new": verdict.new, "crash": verdict.crash}
    return response
