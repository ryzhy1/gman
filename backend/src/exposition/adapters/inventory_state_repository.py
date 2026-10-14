"""Чтение состояния inventory для /metrics: источники, экземпляры, Events узлов и последние релизы."""
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from database.column_types import as_utc
from database.tables import InstanceRecord, ReleaseRecord, RuntimeEventMarkRecord, SourceRecord
from database.unit_of_work import UnitOfWork
from exposition.entity import (GpuReading, InstanceReading, NodeEventTime, NodeReading, ReleaseReading,
                               SourceReading)



class InventoryStateRepository:
    """Строит сущности exposition из сохраненных снимков и релизов."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def sources(self) -> list[SourceReading]:
        """Последние снимки всех источников по их ID."""
        query = select(SourceRecord).order_by(SourceRecord.source_id)
        readings = []
        for record in self._unit_of_work.session.scalars(query):
            reading = _source_reading(record)
            readings.append(reading)
        return readings

    def instances(self) -> list[InstanceReading]:
        """Наблюдаемые сейчас экземпляры в порядке снимков."""
        query = select(InstanceRecord).order_by(InstanceRecord.source_id, InstanceRecord.position)
        readings = []
        for record in self._unit_of_work.session.scalars(query):
            reading = _instance_reading(record)
            readings.append(reading)
        return readings

    def node_event_times(self) -> list[NodeEventTime]:
        """Последнее время Event узла по источнику, узлу и причине (метки еще в сроке хранения)."""
        mark = RuntimeEventMarkRecord
        query = (select(mark.source_id, mark.node, mark.reason, func.max(mark.last_at)).where(mark.node != "")
                 .group_by(mark.source_id, mark.node, mark.reason).order_by(mark.source_id, mark.node, mark.reason))
        times = []
        for source_id, node, reason, last_at in self._unit_of_work.session.execute(query):
            event_time = NodeEventTime(source_id=source_id, node=node, reason=reason, last_at=as_utc(last_at))
            times.append(event_time)
        return times

    def latest_releases(self) -> list[ReleaseReading]:
        """Последний релиз каждой пары сервис/окружение: по одному ряду, какой бы длинной ни была история."""
        newer = aliased(ReleaseRecord)
        release = ReleaseRecord
        is_newer = or_(newer.started_at > release.started_at,
                       and_(newer.started_at == release.started_at, newer.id > release.id))
        has_newer = exists().where(newer.service_id == release.service_id,
                                   newer.environment == release.environment, is_newer)
        query = select(release).where(~has_newer).order_by(release.service_id, release.environment)
        readings = []
        for record in self._unit_of_work.session.scalars(query):
            reading = ReleaseReading(service_id=record.service_id, environment=record.environment,
                                     status=record.status, started_at=record.started_at)
            readings.append(reading)
        return readings


def _source_reading(record: SourceRecord) -> SourceReading:
    """Источник из записи: число ошибок сбора, узлы и GPU снимка."""
    snapshot = record.snapshot or {}
    nodes = [_node_reading(node) for node in snapshot.get("nodes") or []]
    gpus = [_gpu_reading(gpu) for gpu in snapshot.get("gpus") or []]
    reading = SourceReading(source_id=record.source_id, source_type=record.source_type, hostname=record.hostname,
                            received_at=record.received_at, error_count=len(snapshot.get("errors") or []),
                            nodes=nodes, gpus=gpus)
    return reading


def _node_reading(node: dict[str, Any]) -> NodeReading:
    """Узел из снимка кластера."""
    reading = NodeReading(name=node["name"], ready=bool(node["ready"]), pressure=list(node.get("pressure") or []))
    return reading


def _gpu_reading(gpu: dict[str, Any]) -> GpuReading:
    """GPU из снимка источника."""
    throttle = gpu["throttle_reasons"] if "throttle_reasons" in gpu else None
    reading = GpuReading(id=gpu["id"], vendor=gpu.get("vendor", ""), model=gpu.get("model") or "",
                         temperature_c=gpu.get("temperature_c"),
                         ecc_uncorrected_errors=gpu.get("ecc_uncorrected_errors"), throttle_reasons=throttle)
    return reading


def _instance_reading(record: InstanceRecord) -> InstanceReading:
    """Экземпляр из записи; время OOM разбирается из ISO-строки снимка."""
    data = record.data
    oom_text = data.get("last_oom_at")
    reading = InstanceReading(source_id=record.source_id, instance_id=record.instance_id,
                              name=data.get("name", ""), runtime=data.get("runtime", ""),
                              node=data.get("node", ""), namespace=data.get("namespace", ""),
                              service_id=data.get("service_id", ""), environment=data.get("environment", ""),
                              restarts=data.get("restarts") or 0, reason=data.get("reason") or "",
                              last_termination_reason=data.get("last_termination_reason") or "",
                              last_oom_at=datetime.fromisoformat(oom_text) if oom_text else None)
    return reading
