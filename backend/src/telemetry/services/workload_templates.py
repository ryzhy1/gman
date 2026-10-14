"""Шаблоны PromQL состояния workloads Kubernetes из kube-state-metrics.

Workloads имеют ключ, как правила записи группы gman-workloads в deploy/alerts/rules.yaml: source_id (кластер),
namespace, workload_kind, workload. service_id/environment берутся из таблицы соответствия
gman:kube_workload_service:info, которая несет и матчеры запроса, поэтому каждый шаблон фильтруется
по сервису, окружению или кластеру на стороне Prometheus.
"""
from collections.abc import Callable
from dataclasses import dataclass

from telemetry.services.promql_templates import LabelSelector

WORKLOAD_KEY = "source_id, namespace, workload_kind, workload"
OBJECT_LABEL = {"Deployment": "deployment", "StatefulSet": "statefulset", "DaemonSet": "daemonset", "Job": "job_name"}
FILTERS = {"service_id", "environment", "source_id"}
KIND_SEPARATOR = "|"

Template = Callable[[LabelSelector], str]


@dataclass(frozen=True)
class WorkloadTemplate:
    """Шаблон показания; при непустых extra_labels значение равно 1, а данные несут метки."""

    template: Template
    extra_labels: tuple[str, ...] = ()


def _kinds(metrics: dict[str, str], extra: tuple[str, ...] = ()) -> str:
    """Объединение по видам объектов `max by` объекта, переименованное в ключ workload правил записи.

    `max by` схлопывает ряды старого и нового pod-а kube-state-metrics, которые пересекаются после переезда.
    """
    parts = []
    for kind, metric in metrics.items():
        label = OBJECT_LABEL[kind]
        by = ", ".join(("source_id", "namespace", label) + extra)
        parts.append(f'label_replace(label_replace(max by ({by}) ({metric}), "workload", "$1", "{label}", "(.+)"), '
                     f'"workload_kind", "{kind}", "workload", ".*")')
    expression = " or ".join(parts)
    return expression


def _joined(inner: str, extra: tuple[str, ...] = ()) -> Template:
    """Присоединяет service_id/environment и применяет матчеры запроса через таблицу соответствия."""
    by = ", ".join((WORKLOAD_KEY, "service_id, environment") + extra)

    def render(s: LabelSelector) -> str:
        """Выражение с селектором запроса на таблице соответствия."""
        expression = (f"max by ({by}) (({inner}) * on ({WORKLOAD_KEY}) group_left (service_id, environment) "
                      f"gman:kube_workload_service:info{s()})")
        return expression
    return render


def _per_kind(deployment: str = "", statefulset: str = "", daemonset: str = "", job: str = "",
              extra: tuple[str, ...] = ()) -> Template:
    """Шаблон из метрик отдельных видов объектов (пустая метрика — вид не участвует)."""
    names = {"Deployment": deployment, "StatefulSet": statefulset, "DaemonSet": daemonset, "Job": job}
    present = {kind: metric for kind, metric in names.items() if metric}
    template = _joined(_kinds(present, extra), extra)
    return template


def _pending_pods() -> Template:
    """Pod-ы в Pending сейчас, любого возраста (алерт GManWorkloadPodsPending ждет 10 минут)."""
    inner = (f"count by ({WORKLOAD_KEY}) (gman:kube_pod_workload:info and on (source_id, namespace, pod) "
             '(max by (source_id, namespace, pod) (kube_pod_status_phase{phase="Pending"}) == 1))')
    template = _joined(inner)
    return template


def _firing_alerts() -> Template:
    """Сработавшие алерты workload-ов: их `for` решает, когда rollout считается застрявшим."""
    labels = ("alertname", "severity", "reason")
    inner = f'max by ({WORKLOAD_KEY}, alertname, severity, reason) (ALERTS{{alertstate="firing", scope="workload"}})'
    template = _joined(inner, labels)
    return template


def workload_index(s: LabelSelector) -> str:
    """Список workload-ов, подходящих под матчеры запроса, с их service_id/environment."""
    kinds = KIND_SEPARATOR.join(OBJECT_LABEL)
    expression = f"gman:kube_workload_service:info{s(workload_kind__re=kinds)}"
    return expression


TEMPLATES = {
    "desired": WorkloadTemplate(_per_kind("kube_deployment_spec_replicas", "kube_statefulset_replicas",
                                          "kube_daemonset_status_desired_number_scheduled")),
    "ready": WorkloadTemplate(_per_kind("kube_deployment_status_replicas_ready",
                                        "kube_statefulset_status_replicas_ready",
                                        "kube_daemonset_status_number_ready")),
    "available": WorkloadTemplate(_per_kind("kube_deployment_status_replicas_available",
                                            "kube_statefulset_status_replicas_available",
                                            "kube_daemonset_status_number_available")),
    "updated": WorkloadTemplate(_per_kind("kube_deployment_status_replicas_updated",
                                          "kube_statefulset_status_replicas_updated",
                                          "kube_daemonset_status_updated_number_scheduled")),
    "unavailable": WorkloadTemplate(_per_kind("kube_deployment_status_replicas_unavailable",
                                              daemonset="kube_daemonset_status_number_unavailable")),
    "generation": WorkloadTemplate(_per_kind("kube_deployment_metadata_generation",
                                             "kube_statefulset_metadata_generation",
                                             "kube_daemonset_metadata_generation")),
    "observed_generation": WorkloadTemplate(_per_kind("kube_deployment_status_observed_generation",
                                                      "kube_statefulset_status_observed_generation",
                                                      "kube_daemonset_status_observed_generation")),
    "paused": WorkloadTemplate(_per_kind("kube_deployment_spec_paused")),
    "job_failed": WorkloadTemplate(_per_kind(job='kube_job_failed{condition="true"}')),
    "job_active": WorkloadTemplate(_per_kind(job="kube_job_status_active")),
    "pending_pods": WorkloadTemplate(_pending_pods()),
    "progressing": WorkloadTemplate(_per_kind('kube_deployment_status_condition{condition="Progressing"} == 1',
                                              extra=("status", "reason")), ("status", "reason")),
    "current_revision": WorkloadTemplate(_per_kind(statefulset="kube_statefulset_status_current_revision",
                                                   extra=("revision",)), ("revision",)),
    "update_revision": WorkloadTemplate(_per_kind(statefulset="kube_statefulset_status_update_revision",
                                                  extra=("revision",)), ("revision",)),
    "alerts": WorkloadTemplate(_firing_alerts(), ("alertname", "severity", "reason")),
}
COUNTS = ("desired", "ready", "available", "updated", "unavailable", "generation", "observed_generation",
          "pending_pods")
