"""State of Kubernetes workloads from kube-state-metrics, read from Prometheus with fixed query templates.

Workloads are keyed like the recording rules of group gman-workloads in deploy/alerts/rules.yaml:
source_id (cluster), namespace, workload_kind, workload. service_id/environment come from the join table
gman:kube_workload_service:info, which also carries the request's matchers, so every template is filtered
by service, environment or cluster on the Prometheus side. Clients never send PromQL.
"""
from concurrent.futures import ThreadPoolExecutor

from .metrics import LABEL, number, request, selector

WK = "source_id, namespace, workload_kind, workload"
OBJECT_LABEL = {"Deployment": "deployment", "StatefulSet": "statefulset", "DaemonSet": "daemonset", "Job": "job_name"}
FILTERS = {"service_id", "environment", "source_id"}


def kinds(metrics, extra=()):
    """Union over kinds of `max by` the object (series of an old and a new kube-state-metrics pod overlap
    after it moves), relabelled to the workload key of the recording rules."""
    parts = []
    for kind, metric in metrics.items():
        label = OBJECT_LABEL[kind]
        by = ", ".join(("source_id", "namespace", label) + extra)
        parts.append(f'label_replace(label_replace(max by ({by}) ({metric}), "workload", "$1", "{label}", "(.+)"), '
                     f'"workload_kind", "{kind}", "workload", ".*")')
    return " or ".join(parts)


def joined(inner, extra=()):
    """Attach service_id/environment and apply the request's matchers through the join table."""
    by = ", ".join((WK, "service_id, environment") + extra)
    return lambda s: (f"max by ({by}) (({inner}) * on ({WK}) group_left (service_id, environment) "
                      f"gman:kube_workload_service:info{s()})")


def per_kind(deployment=None, statefulset=None, daemonset=None, job=None, extra=()):
    names = {"Deployment": deployment, "StatefulSet": statefulset, "DaemonSet": daemonset, "Job": job}
    return joined(kinds({k: v for k, v in names.items() if v}, extra), extra)


# name: (template, extra labels). Values of templates with extra labels are 1; the labels carry the data.
TEMPLATES = {
    "desired": (per_kind("kube_deployment_spec_replicas", "kube_statefulset_replicas",
                         "kube_daemonset_status_desired_number_scheduled"), ()),
    "ready": (per_kind("kube_deployment_status_replicas_ready", "kube_statefulset_status_replicas_ready",
                       "kube_daemonset_status_number_ready"), ()),
    "available": (per_kind("kube_deployment_status_replicas_available", "kube_statefulset_status_replicas_available",
                           "kube_daemonset_status_number_available"), ()),
    "updated": (per_kind("kube_deployment_status_replicas_updated", "kube_statefulset_status_replicas_updated",
                         "kube_daemonset_status_updated_number_scheduled"), ()),
    "unavailable": (per_kind("kube_deployment_status_replicas_unavailable",
                             daemonset="kube_daemonset_status_number_unavailable"), ()),
    "generation": (per_kind("kube_deployment_metadata_generation", "kube_statefulset_metadata_generation",
                            "kube_daemonset_metadata_generation"), ()),
    "observed_generation": (per_kind("kube_deployment_status_observed_generation",
                                     "kube_statefulset_status_observed_generation",
                                     "kube_daemonset_status_observed_generation"), ()),
    "paused": (per_kind("kube_deployment_spec_paused"), ()),
    "job_failed": (per_kind(job='kube_job_failed{condition="true"}'), ()),
    "job_active": (per_kind(job="kube_job_status_active"), ()),
    # Pods Pending now, of any age (the alert GManWorkloadPodsPending waits 10 minutes).
    "pending_pods": (joined(f"count by ({WK}) (gman:kube_pod_workload:info and on (source_id, namespace, pod) "
                            '(max by (source_id, namespace, pod) (kube_pod_status_phase{phase="Pending"}) == 1))'), ()),
    "progressing": (per_kind('kube_deployment_status_condition{condition="Progressing"} == 1',
                             extra=("status", "reason")), ("status", "reason")),
    "current_revision": (per_kind(statefulset="kube_statefulset_status_current_revision", extra=("revision",)),
                         ("revision",)),
    "update_revision": (per_kind(statefulset="kube_statefulset_status_update_revision", extra=("revision",)),
                        ("revision",)),
    # Firing workload alerts: their `for` durations decide when a rollout counts as stuck.
    "alerts": (joined(f'max by ({WK}, alertname, severity, reason) (ALERTS{{alertstate="firing", scope="workload"}})',
                      ("alertname", "severity", "reason")), ("alertname", "severity", "reason")),
}
COUNTS = ("desired", "ready", "available", "updated", "unavailable", "generation", "observed_generation",
          "pending_pods")
ORDER = {"stuck": 0, "degraded": 1, "rolling": 2, "unknown": 3, "ok": 4}


def classify(w):
    """ok / rolling / degraded / stuck (unknown without replica metrics) and the reasons shown to people."""
    stuck = [a["name"] + (f" ({a['reason']})" if a["reason"] else "") for a in w["alerts"]
             if a["name"] == "GManWorkloadRolloutStuck"]
    if w["kind"] == "Deployment" and w["progressing"] is False and not stuck:
        stuck.append(f"Progressing=False ({w['progressing_reason'] or 'без причины'})")
    if stuck:
        return "stuck", stuck
    alerts = [a["name"] for a in w["alerts"]]
    if w["kind"] == "Job":
        if w["failed"]:
            return "degraded", ["Job завершилась ошибкой (backoffLimit или activeDeadlineSeconds)"]
        return "ok", [f"выполняется, pod-ов — {w['active']}"] if w["active"] else []
    desired, available, updated = w["desired"], w["available"], w["updated"]
    if desired is None:
        return "unknown", ["нет метрик kube-state-metrics"]
    rolling = []
    if not w["paused"]:
        if None not in (w["generation"], w["observed_generation"]) and w["generation"] > w["observed_generation"]:
            rolling.append("контроллер еще не обработал новую спецификацию")
        if w["kind"] == "StatefulSet":
            # OnDelete never advances current_revision: a finished OnDelete rollout has updated = desired.
            if w["update_revision"] and w["current_revision"] != w["update_revision"] \
                    and updated is not None and updated < desired:
                rolling.append(f"на новой ревизии {updated} из {desired} (при OnDelete — после удаления pod-ов)")
        elif updated is not None and updated < desired:
            rolling.append(f"обновлено {updated} из {desired}")
        elif w["kind"] == "Deployment" and w["progressing"] and w["progressing_reason"] == "ReplicaSetUpdated":
            rolling.append("заменяются pod-ы прежней версии")
    if desired > 0 and available == 0:
        return "degraded", ["нет доступных реплик"] + rolling + alerts
    if rolling:
        return "rolling", rolling
    degraded = []
    if available is not None and available < desired:
        degraded.append(f"доступно {available} из {desired}")
    elif w["unavailable"]:
        degraded.append(f"недоступно — {w['unavailable']}")
    if w["pending_pods"]:
        degraded.append(f"pod-ов в Pending — {w['pending_pods']}")
    degraded += alerts
    if degraded:
        return "degraded", degraded
    return "ok", ["rollout приостановлен (paused)"] if w["paused"] else []


def workloads(matchers):
    """Deployments, StatefulSets, DaemonSets and running or failed Jobs matching service_id/environment/source_id."""
    if not matchers or set(matchers) - FILTERS:
        raise KeyError("Unknown filter")
    for value in matchers.values():
        if not LABEL.match(value):
            raise KeyError("Invalid identifier")
    s = selector(matchers)
    jobs = [("", lambda s: f'gman:kube_workload_service:info{s(workload_kind__re="|".join(OBJECT_LABEL))}', ())]
    jobs += [(name, template, labels) for name, (template, labels) in TEMPLATES.items()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: request("/api/v1/query", {"query": job[1](s)}), jobs))
    found, values, labeled = {}, {}, {}
    for (name, _, labels), series in zip(jobs, results):
        for item in series:
            m = item["metric"]
            key = (m.get("source_id", ""), m.get("namespace", ""), m.get("workload_kind", ""), m.get("workload", ""))
            if not name:
                found[key] = {"cluster": key[0], "namespace": key[1], "kind": key[2], "name": key[3],
                              "service_id": m.get("service_id", ""), "environment": m.get("environment", "")}
            elif labels:
                labeled.setdefault(key, {}).setdefault(name, []).append({k: m.get(k, "") for k in labels})
            else:
                value = number(item["value"][1])
                values.setdefault(key, {})[name] = None if value is None else int(value)
    result = []
    for key, entry in found.items():
        v, l = values.get(key, {}), labeled.get(key, {})
        conditions = l.get("progressing", [])
        # A Deployment has one Progressing condition; prefer "true" if two kube-state-metrics pods disagree.
        condition = next((c for c in conditions if c["status"] == "true"), conditions[0] if conditions else None)
        job = entry["kind"] == "Job"
        w = {**entry, **{name: v.get(name) for name in COUNTS},
             "paused": bool(v.get("paused")),
             "progressing": None if condition is None else condition["status"] == "true",
             "progressing_reason": condition["reason"] if condition else "",
             "current_revision": (l.get("current_revision") or [{"revision": ""}])[0]["revision"],
             "update_revision": (l.get("update_revision") or [{"revision": ""}])[0]["revision"],
             "failed": bool(v.get("job_failed")) if job else None,
             "active": v.get("job_active") if job else None,
             "alerts": sorted(({"name": a["alertname"], "severity": a["severity"], "reason": a["reason"]}
                               for a in l.get("alerts", [])), key=lambda a: (a["name"], a["reason"]))}
        if w["kind"] == "StatefulSet" and w["available"] is None:
            w["available"] = w["ready"]
        # Completed Jobs (CronJob history) would crowd the card: only running and failed ones are listed.
        if job and not w["failed"] and not w["active"] and not w["alerts"]:
            continue
        w["status"], w["reasons"] = classify(w)
        result.append(w)
    return sorted(result, key=lambda w: (ORDER[w["status"]], w["cluster"], w["namespace"], w["kind"], w["name"]))
