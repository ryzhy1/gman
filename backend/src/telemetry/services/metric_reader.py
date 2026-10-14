"""Чтение метрик ресурсов по фиксированным шаблонам: последние значения и ряды за период."""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from telemetry.adapters.prometheus_client import PrometheusClient
from telemetry.entity import RangeQuery, RangeSeries, Sample, UnknownQueryError
from telemetry.services.promql_templates import LABEL_VALUE, RANGE_SECONDS, SCOPES, LabelSelector, Template

MIN_STEP_SECONDS = 15
POINTS_PER_RANGE = 240

CurrentValues = dict[str, dict[str, dict[str, float | None]]]


@dataclass(frozen=True)
class _CurrentJob:
    """Один запрос последних значений: область, метрика и выражение."""

    scope: str
    metric: str
    promql: str


class MetricReader:
    """Последние значения всех шаблонов и ряд одного шаблона для одного объекта."""

    def __init__(self, prometheus: PrometheusClient, max_workers: int) -> None:
        """Запоминает клиент Prometheus и число параллельных запросов."""
        self._prometheus = prometheus
        self._max_workers = max_workers

    @property
    def enabled(self) -> bool:
        """Настроен ли Prometheus."""
        enabled = self._prometheus.enabled
        return enabled

    def current(self) -> CurrentValues:
        """Значения по областям и объектам: ключ объекта — метки ключа области через ее разделитель."""
        jobs = _current_jobs()
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            results = list(pool.map(self._run, jobs))
        data: CurrentValues = {}
        for scope in SCOPES:
            data[scope] = {}
        for job, samples in zip(jobs, results):
            scope = SCOPES[job.scope]
            for sample in samples:
                key = scope.key_separator.join(sample.labels.get(label, "") for label in scope.key_labels)
                data[job.scope].setdefault(key, {})[job.metric] = sample.value
        return data

    def series(self, query: RangeQuery) -> RangeSeries:
        """Ряд метрики объекта за период; неизвестный шаблон или объект — UnknownQueryError."""
        template = _validated_template(query)
        seconds = RANGE_SECONDS[query.range_name]
        end = time.time()
        step = max(MIN_STEP_SECONDS, seconds // POINTS_PER_RANGE)
        selector = LabelSelector(query.matchers)
        points = self._prometheus.first_series(template(selector), end - seconds, end, step)
        series = RangeSeries(scope=query.scope, metric=query.metric, range_name=query.range_name, step=step,
                             points=points)
        return series

    def _run(self, job: _CurrentJob) -> list[Sample]:
        """Выполняет один запрос последних значений."""
        samples = self._prometheus.instant(job.promql)
        return samples


def _current_jobs() -> list[_CurrentJob]:
    """Запросы последних значений всех шаблонов всех областей без матчеров."""
    selector = LabelSelector({})
    jobs = []
    for scope_name, scope in SCOPES.items():
        for metric, template in scope.templates.items():
            job = _CurrentJob(scope=scope_name, metric=metric, promql=template(selector))
            jobs.append(job)
    return jobs


def _validated_template(query: RangeQuery) -> Template:
    """Шаблон запроса, если область, метрика, диапазон и метки объекта допустимы."""
    scope = SCOPES[query.scope]
    known = query.metric in scope.templates and query.range_name in RANGE_SECONDS
    if not known or set(query.matchers) != set(scope.key_labels):
        raise UnknownQueryError("Unknown metric, range or object")
    for value in query.matchers.values():
        if not LABEL_VALUE.match(value):
            raise UnknownQueryError("Invalid identifier")
    template = scope.templates[query.metric]
    return template
