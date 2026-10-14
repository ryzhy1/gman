"""Статус workload-а для портала: ok / rolling / degraded / stuck (unknown без метрик реплик) и причины."""
from telemetry.entity import Workload

STUCK = "stuck"
DEGRADED = "degraded"
ROLLING = "rolling"
UNKNOWN = "unknown"
OK = "ok"
STATUS_ORDER = {STUCK: 0, DEGRADED: 1, ROLLING: 2, UNKNOWN: 3, OK: 4}
DEPLOYMENT = "Deployment"
STATEFUL_SET = "StatefulSet"
JOB = "Job"
ROLLOUT_STUCK_ALERT = "GManWorkloadRolloutStuck"
REPLICA_SET_UPDATED = "ReplicaSetUpdated"

Verdict = tuple[str, list[str]]


class WorkloadClassifier:
    """Выводит статус workload-а и человекочитаемые причины."""

    def classify(self, workload: Workload) -> Verdict:
        """Статус и причины; застревание важнее деградации, деградация — раскатки."""
        stuck = _stuck_reasons(workload)
        if stuck:
            verdict = (STUCK, stuck)
            return verdict
        if workload.kind == JOB:
            verdict = _job_verdict(workload)
            return verdict
        if workload.desired is None:
            verdict = (UNKNOWN, ["нет метрик kube-state-metrics"])
            return verdict
        verdict = _replica_verdict(workload)
        return verdict


def _stuck_reasons(workload: Workload) -> list[str]:
    """Причины застревания: алерт GManWorkloadRolloutStuck или Progressing=False у Deployment."""
    stuck = []
    for alert in workload.alerts:
        if alert.name == ROLLOUT_STUCK_ALERT:
            stuck.append(alert.name + (f" ({alert.reason})" if alert.reason else ""))
    if workload.kind == DEPLOYMENT and workload.progressing is False and not stuck:
        stuck.append(f"Progressing=False ({workload.progressing_reason or 'без причины'})")
    return stuck


def _job_verdict(workload: Workload) -> Verdict:
    """Job: ошибка — деградация, иначе ok (с числом выполняющихся pod-ов)."""
    if workload.failed:
        verdict = (DEGRADED, ["Job завершилась ошибкой (backoffLimit или activeDeadlineSeconds)"])
        return verdict
    reasons = [f"выполняется, pod-ов — {workload.active}"] if workload.active else []
    verdict = (OK, reasons)
    return verdict


def _replica_verdict(workload: Workload) -> Verdict:
    """Deployment, StatefulSet и DaemonSet по числам реплик, ревизиям и алертам."""
    rolling = [] if workload.paused else _rolling_reasons(workload)
    alerts = [alert.name for alert in workload.alerts]
    if workload.desired > 0 and workload.available == 0:
        verdict = (DEGRADED, ["нет доступных реплик"] + rolling + alerts)
        return verdict
    if rolling:
        verdict = (ROLLING, rolling)
        return verdict
    degraded = _degraded_reasons(workload) + alerts
    if degraded:
        verdict = (DEGRADED, degraded)
        return verdict
    verdict = (OK, ["rollout приостановлен (paused)"] if workload.paused else [])
    return verdict


def _rolling_reasons(workload: Workload) -> list[str]:
    """Признаки идущей раскатки: новая спецификация не обработана или реплики еще не обновлены."""
    rolling = []
    generation, observed = workload.generation, workload.observed_generation
    if generation is not None and observed is not None and generation > observed:
        rolling.append("контроллер еще не обработал новую спецификацию")
    if workload.kind == STATEFUL_SET:
        rolling += _stateful_set_rolling(workload)
    elif workload.updated is not None and workload.updated < workload.desired:
        rolling.append(f"обновлено {workload.updated} из {workload.desired}")
    elif workload.kind == DEPLOYMENT and workload.progressing and workload.progressing_reason == REPLICA_SET_UPDATED:
        rolling.append("заменяются pod-ы прежней версии")
    return rolling


def _stateful_set_rolling(workload: Workload) -> list[str]:
    """StatefulSet: OnDelete не двигает current_revision, поэтому завершенная раскатка — updated = desired."""
    revision_differs = workload.update_revision and workload.current_revision != workload.update_revision
    behind = workload.updated is not None and workload.updated < workload.desired
    if revision_differs and behind:
        reasons = [f"на новой ревизии {workload.updated} из {workload.desired} (при OnDelete — после удаления pod-ов)"]
        return reasons
    return []


def _degraded_reasons(workload: Workload) -> list[str]:
    """Нехватка доступных реплик и pod-ы в Pending."""
    degraded = []
    if workload.available is not None and workload.available < workload.desired:
        degraded.append(f"доступно {workload.available} из {workload.desired}")
    elif workload.unavailable:
        degraded.append(f"недоступно — {workload.unavailable}")
    if workload.pending_pods:
        degraded.append(f"pod-ов в Pending — {workload.pending_pods}")
    return degraded
