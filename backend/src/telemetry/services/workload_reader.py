"""Состояние workloads Kubernetes: параллельные запросы шаблонов и сборка показаний по ключу workload-а."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from telemetry.adapters.prometheus_client import PrometheusClient
from telemetry.entity import Sample, UnknownQueryError, Workload, WorkloadAlert, WorkloadReadings
from telemetry.services.promql_templates import LABEL_VALUE, LabelSelector
from telemetry.services.workload_classifier import STATEFUL_SET, STATUS_ORDER, JOB, WorkloadClassifier
from telemetry.services.workload_templates import COUNTS, FILTERS, TEMPLATES, WorkloadTemplate, workload_index

TRUE = "true"
INDEX_NAME = ""

WorkloadKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class _Job:
    """Один запрос шаблона: имя показания (пустое — список workload-ов) и шаблон."""

    name: str
    template: WorkloadTemplate


class WorkloadReader:
    """Deployments, StatefulSets, DaemonSets и выполняющиеся или упавшие Jobs по сервису, окружению или кластеру."""

    def __init__(self, prometheus: PrometheusClient, classifier: WorkloadClassifier, max_workers: int) -> None:
        """Запоминает клиент Prometheus, классификатор и число параллельных запросов."""
        self._prometheus = prometheus
        self._classifier = classifier
        self._max_workers = max_workers

    @property
    def enabled(self) -> bool:
        """Настроен ли Prometheus."""
        enabled = self._prometheus.enabled
        return enabled

    def workloads(self, matchers: dict[str, str]) -> list[Workload]:
        """Workloads под матчерами запроса, сначала проблемные."""
        _validate(matchers)
        selector = LabelSelector(matchers)
        jobs = _jobs()
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            results = list(pool.map(lambda job: self._prometheus.instant(job.template.template(selector)), jobs))
        found, readings = _collect(jobs, results)
        result = []
        empty = WorkloadReadings()
        for key, workload in found.items():
            _apply_readings(workload, readings.get(key, empty))
            # Завершенные Jobs (история CronJob) засорили бы карточку: показываются выполняющиеся и упавшие.
            if workload.kind == JOB and not workload.failed and not workload.active and not workload.alerts:
                continue
            workload.status, workload.reasons = self._classifier.classify(workload)
            result.append(workload)
        result.sort(key=_order)
        return result


def _validate(matchers: dict[str, str]) -> None:
    """Допустимы только фильтры service_id/environment/source_id со значениями-идентификаторами."""
    if not matchers or set(matchers) - FILTERS:
        raise UnknownQueryError("Unknown filter")
    for value in matchers.values():
        if not LABEL_VALUE.match(value):
            raise UnknownQueryError("Invalid identifier")


def _jobs() -> list[_Job]:
    """Список workload-ов, затем по запросу на каждое показание."""
    index = WorkloadTemplate(workload_index)
    jobs = [_Job(name=INDEX_NAME, template=index)]
    for name, template in TEMPLATES.items():
        job = _Job(name=name, template=template)
        jobs.append(job)
    return jobs


def _key(labels: dict[str, str]) -> WorkloadKey:
    """Ключ workload-а, как в правилах записи."""
    key = (labels.get("source_id", ""), labels.get("namespace", ""), labels.get("workload_kind", ""),
           labels.get("workload", ""))
    return key


def _collect(jobs: list[_Job], results: list[list[Sample]]) -> tuple[dict[WorkloadKey, Workload],
                                                                     dict[WorkloadKey, WorkloadReadings]]:
    """Раскладывает ответы шаблонов: найденные workloads и их показания по ключу."""
    found: dict[WorkloadKey, Workload] = {}
    readings: dict[WorkloadKey, WorkloadReadings] = {}
    for job, samples in zip(jobs, results):
        for sample in samples:
            key = _key(sample.labels)
            if job.name == INDEX_NAME:
                found[key] = _workload(key, sample.labels)
                continue
            if key not in readings:
                readings[key] = WorkloadReadings()
            _add_reading(readings[key], job, sample)
    collected = (found, readings)
    return collected


def _workload(key: WorkloadKey, labels: dict[str, str]) -> Workload:
    """Workload из строки таблицы соответствия."""
    workload = Workload(cluster=key[0], namespace=key[1], kind=key[2], name=key[3],
                        service_id=labels.get("service_id", ""), environment=labels.get("environment", ""))
    return workload


def _add_reading(readings: WorkloadReadings, job: _Job, sample: Sample) -> None:
    """Число — в counts; показания с метками — в labeled (их значение всегда 1)."""
    if job.template.extra_labels:
        labels = {label: sample.labels.get(label, "") for label in job.template.extra_labels}
        readings.labeled.setdefault(job.name, []).append(labels)
        return
    readings.counts[job.name] = None if sample.value is None else int(sample.value)


def _apply_readings(workload: Workload, readings: WorkloadReadings) -> None:
    """Переносит показания в workload."""
    counts = readings.counts
    for name in COUNTS:
        setattr(workload, name, counts.get(name))
    workload.paused = bool(counts.get("paused"))
    _apply_progressing(workload, readings.labeled.get("progressing", []))
    workload.current_revision = _revision(readings.labeled.get("current_revision"))
    workload.update_revision = _revision(readings.labeled.get("update_revision"))
    if workload.kind == JOB:
        workload.failed = bool(counts.get("job_failed"))
        workload.active = counts.get("job_active")
    workload.alerts = _alerts(readings.labeled.get("alerts", []))
    if workload.kind == STATEFUL_SET and workload.available is None:
        workload.available = workload.ready


def _apply_progressing(workload: Workload, conditions: list[dict[str, str]]) -> None:
    """Условие Progressing Deployment-а; при расхождении двух pod-ов kube-state-metrics выигрывает "true"."""
    condition = next((c for c in conditions if c["status"] == TRUE), conditions[0] if conditions else None)
    if condition is None:
        return
    workload.progressing = condition["status"] == TRUE
    workload.progressing_reason = condition["reason"]


def _revision(values: list[dict[str, str]] | None) -> str:
    """Ревизия StatefulSet или пустая строка."""
    revision = values[0]["revision"] if values else ""
    return revision


def _alerts(values: list[dict[str, str]]) -> list[WorkloadAlert]:
    """Сработавшие алерты workload-а, упорядоченные по имени и причине."""
    alerts = []
    for value in values:
        alert = WorkloadAlert(name=value["alertname"], severity=value["severity"], reason=value["reason"])
        alerts.append(alert)
    alerts.sort(key=lambda alert: (alert.name, alert.reason))
    return alerts


def _order(workload: Workload) -> tuple[int, str, str, str, str]:
    """Порядок карточек: сначала проблемные, затем по кластеру, namespace, виду и имени."""
    order = (STATUS_ORDER[workload.status], workload.cluster, workload.namespace, workload.kind, workload.name)
    return order
