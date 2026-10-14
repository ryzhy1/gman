"""Хранение каталога сервисов (таблица services)."""
from sqlalchemy import select

from database.tables import ServiceRecord
from database.unit_of_work import UnitOfWork
from inventory.entity import CatalogService

# Поля сервиса, у которых есть колонка; остальное хранится в extra.
COLUMNS = ("name", "description", "owner", "team", "repository", "grafana", "splunk", "bamboo")


class CatalogRepository:
    """Чтение и запись сервисов каталога."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        """Запоминает границу транзакции, из которой берется сессия."""
        self._unit_of_work = unit_of_work

    def get(self, service_id: str) -> CatalogService | None:
        """Сервис по ID или None."""
        record = self._unit_of_work.session.get(ServiceRecord, service_id)
        if record is None:
            return None
        service = _service(record)
        return service

    def list_all(self) -> list[CatalogService]:
        """Все сервисы по ID."""
        query = select(ServiceRecord).order_by(ServiceRecord.id)
        services = []
        for record in self._unit_of_work.session.scalars(query):
            service = _service(record)
            services.append(service)
        return services

    def put(self, service: CatalogService) -> None:
        """Создает или заменяет сервис."""
        session = self._unit_of_work.session
        record = session.get(ServiceRecord, service.id)
        if record is None:
            record = ServiceRecord(id=service.id)
            session.add(record)
        for column in COLUMNS:
            setattr(record, column, getattr(service, column))
        record.extra = dict(service.extra)


def _service(record: ServiceRecord) -> CatalogService:
    """Сущность сервиса из записи."""
    service = CatalogService(id=record.id, name=record.name, description=record.description, owner=record.owner,
                             team=record.team, repository=record.repository, grafana=record.grafana,
                             splunk=record.splunk, bamboo=record.bamboo, extra=dict(record.extra or {}))
    return service
