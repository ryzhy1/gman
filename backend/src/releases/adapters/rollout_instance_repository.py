"""Наблюдаемые экземпляры сервиса в окружении (таблица instances) как экземпляры раскатки."""
from typing import Any

from sqlalchemy import select

from database.tables import InstanceRecord
from database.unit_of_work import UnitOfWork
from releases.entity import RolloutInstance


class RolloutInstanceRepository:
    """Текущие экземпляры пары сервис/окружение."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def current(self, service_id: str, environment: str) -> list[RolloutInstance]:
        """Экземпляры в порядке источников и снимков."""
        query = (select(InstanceRecord)
                 .where(InstanceRecord.service_id == service_id, InstanceRecord.environment == environment)
                 .order_by(InstanceRecord.source_id, InstanceRecord.position))
        instances = []
        for record in self._unit_of_work.session.scalars(query):
            instance = rollout_instance(record.source_id, record.data)
            instances.append(instance)
        return instances


def rollout_instance(source_id: str, document: dict[str, Any]) -> RolloutInstance:
    """Экземпляр раскатки из документа агента."""
    config = document.get("config") or {}
    origin = document.get("origin") or {}
    instance = RolloutInstance(source_id=source_id, id=document["id"], name=document.get("name", ""),
                               environment=document.get("environment", ""), node=document.get("node", ""),
                               status=document.get("status", ""), health=document.get("health", ""),
                               ready=document.get("ready"), reason=document.get("reason") or "",
                               exit_code=document.get("exit_code"),
                               last_termination_reason=document.get("last_termination_reason") or "",
                               restarts=document.get("restarts") or 0, image=config.get("image", ""),
                               image_id=config.get("image_id", ""), image_version=origin.get("image_version", ""),
                               app_version=origin.get("app_version", ""), created_at=document.get("created_at"),
                               started_at=document.get("started_at"))
    return instance
