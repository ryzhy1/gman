import httpx

from backend.app import workloads
from backend.tests.test_api import client  # noqa: F401  (fixture)
from backend.tests.test_metrics import configured, prometheus, vector  # noqa: F401  (fixture)

KEY = {"source_id": "cluster-01", "namespace": "shop"}


def wl(kind, name, **labels):
    return {**KEY, "workload_kind": kind, "workload": name, "service_id": "api", "environment": "prod", **labels}


# Distinctive metric of each template -> series it returns.
ANSWERS = {
    "gman:kube_workload_service:info{workload_kind=~": [
        (wl("Deployment", "api"), "1"), (wl("Deployment", "web"), "1"), (wl("StatefulSet", "db"), "1"),
        (wl("StatefulSet", "queue"), "1"), (wl("DaemonSet", "agent"), "1"), (wl("Job", "report"), "1"),
        (wl("Job", "old"), "1")],
    "kube_deployment_spec_replicas": [(wl("Deployment", "api"), "3"), (wl("Deployment", "web"), "2"),
                                      (wl("StatefulSet", "db"), "3"), (wl("StatefulSet", "queue"), "3"),
                                      (wl("DaemonSet", "agent"), "4")],
    "kube_deployment_status_replicas_ready": [(wl("Deployment", "api"), "3"), (wl("StatefulSet", "db"), "3")],
    "kube_deployment_status_replicas_available": [(wl("Deployment", "api"), "3"), (wl("Deployment", "web"), "1"),
                                                  (wl("StatefulSet", "db"), "3"), (wl("StatefulSet", "queue"), "3"),
                                                  (wl("DaemonSet", "agent"), "3")],
    "kube_deployment_status_replicas_updated": [(wl("Deployment", "api"), "3"), (wl("Deployment", "web"), "2"),
                                                (wl("StatefulSet", "db"), "1"), (wl("StatefulSet", "queue"), "3"),
                                                (wl("DaemonSet", "agent"), "2")],
    "kube_deployment_status_replicas_unavailable": [(wl("DaemonSet", "agent"), "1")],
    "kube_deployment_metadata_generation": [(wl("Deployment", "api"), "4"), (wl("Deployment", "web"), "2")],
    "kube_deployment_status_observed_generation": [(wl("Deployment", "api"), "4"), (wl("Deployment", "web"), "2")],
    "kube_deployment_spec_paused": [(wl("Deployment", "api"), "0"), (wl("Deployment", "web"), "0")],
    'kube_job_failed{condition="true"}': [(wl("Job", "report"), "1"), (wl("Job", "old"), "0")],
    "kube_job_status_active": [(wl("Job", "report"), "0"), (wl("Job", "old"), "0")],
    'kube_pod_status_phase{phase="Pending"}': [(wl("Deployment", "web"), "1")],
    'kube_deployment_status_condition{condition="Progressing"}': [
        (wl("Deployment", "api", status="true", reason="NewReplicaSetAvailable"), "1"),
        (wl("Deployment", "web", status="false", reason="ProgressDeadlineExceeded"), "1")],
    "kube_statefulset_status_current_revision": [(wl("StatefulSet", "db", revision="db-1"), "1"),
                                                 (wl("StatefulSet", "queue", revision="queue-1"), "1")],
    "kube_statefulset_status_update_revision": [(wl("StatefulSet", "db", revision="db-2"), "1"),
                                                (wl("StatefulSet", "queue", revision="queue-2"), "1")],
    "ALERTS{": [(wl("DaemonSet", "agent", alertname="GManWorkloadRolloutStuck", severity="warning",
                    reason="OutdatedReplicas"), "1")],
}


def test_workloads_state_per_service(client, configured):
    seen = []
    def handler(request):
        query = request.url.params["query"]
        seen.append(query)
        for marker, series in ANSWERS.items():
            if marker in query:
                return httpx.Response(200, json=vector(*series))
        return httpx.Response(200, json=vector())
    prometheus(handler)
    params = {"service_id": "api", "environment": "prod"}
    data = client.get("/api/workloads", params=params).json()
    assert data["enabled"] and data["error"] is None
    assert len(seen) == len(workloads.TEMPLATES) + 1
    # Every query is filtered through the join table, never by client-supplied PromQL.
    assert all('gman:kube_workload_service:info{' in q and 'service_id="api",environment="prod"}' in q for q in seen)
    assert any('workload_kind=~"Deployment|StatefulSet|DaemonSet|Job",service_id="api"' in q for q in seen)
    by = {(w["kind"], w["name"]): w for w in data["workloads"]}
    # Completed Job "old" is not listed.
    assert set(by) == {("Deployment", "api"), ("Deployment", "web"), ("StatefulSet", "db"), ("StatefulSet", "queue"),
                       ("DaemonSet", "agent"), ("Job", "report")}
    api = by["Deployment", "api"]
    assert api["status"] == "ok" and api["reasons"] == []
    assert (api["desired"], api["ready"], api["available"], api["updated"]) == (3, 3, 3, 3)
    assert api["progressing"] is True and api["progressing_reason"] == "NewReplicaSetAvailable"
    assert api["cluster"] == "cluster-01" and api["service_id"] == "api" and api["environment"] == "prod"
    web = by["Deployment", "web"]
    assert web["status"] == "stuck" and web["reasons"] == ["Progressing=False (ProgressDeadlineExceeded)"]
    assert web["pending_pods"] == 1
    # StatefulSet rollout: update revision differs and not all replicas are on it.
    db = by["StatefulSet", "db"]
    assert db["status"] == "rolling" and db["current_revision"] == "db-1" and db["update_revision"] == "db-2"
    # OnDelete: current_revision is never advanced, but every replica is updated — the rollout is done.
    assert by["StatefulSet", "queue"]["status"] == "ok"
    agent = by["DaemonSet", "agent"]
    assert agent["status"] == "stuck" and agent["unavailable"] == 1
    assert agent["alerts"] == [{"name": "GManWorkloadRolloutStuck", "severity": "warning", "reason": "OutdatedReplicas"}]
    report = by["Job", "report"]
    assert report["status"] == "degraded" and report["failed"] is True
    assert [w["status"] for w in data["workloads"]] == ["stuck", "stuck", "degraded", "rolling", "ok", "ok"]


def test_workloads_filters_and_errors(client, configured, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.url.params["query"])
        return httpx.Response(200, json=vector())
    prometheus(handler)
    data = client.get("/api/workloads", params={"cluster": "cluster-01"}).json()
    assert data == {"enabled": True, "error": None, "workloads": []}
    assert all('source_id="cluster-01"}' in q and "service_id=" not in q for q in seen)
    for bad in [{}, {"environment": "prod"}, {"service_id": 'a"} or vector(1)'}, {"cluster": "a b"}]:
        assert client.get("/api/workloads", params=bad).status_code == 422, bad
    def down(request):
        raise httpx.ConnectError("refused")
    prometheus(down)
    data = client.get("/api/workloads", params={"service_id": "api"}).json()
    assert data["enabled"] and data["error"].startswith("Metrics store unavailable") and data["workloads"] == []
    monkeypatch.delenv("PROMETHEUS_URL")
    assert client.get("/api/workloads", params={"service_id": "api"}).json() == {
        "enabled": False, "error": "PROMETHEUS_URL is not configured", "workloads": []}


def base(**over):
    w = {"kind": "Deployment", "desired": 3, "ready": 3, "available": 3, "updated": 3, "unavailable": 0,
         "generation": 2, "observed_generation": 2, "pending_pods": None, "paused": False, "progressing": True,
         "progressing_reason": "NewReplicaSetAvailable", "current_revision": "", "update_revision": "",
         "failed": None, "active": None, "alerts": []}
    return {**w, **over}


def test_classify():
    assert workloads.classify(base()) == ("ok", [])
    assert workloads.classify(base(updated=1))[0] == "rolling"
    assert workloads.classify(base(progressing_reason="ReplicaSetUpdated"))[0] == "rolling"
    assert workloads.classify(base(generation=3))[0] == "rolling"
    # A paused Deployment with a pending change is not rolling.
    assert workloads.classify(base(generation=3, updated=1, paused=True)) == ("ok", ["rollout приостановлен (paused)"])
    assert workloads.classify(base(available=2)) == ("degraded", ["доступно 2 из 3"])
    assert workloads.classify(base(available=0, updated=1))[0] == "degraded"
    assert workloads.classify(base(pending_pods=2))[0] == "degraded"
    assert workloads.classify(base(alerts=[{"name": "GManDaemonSetMisscheduled", "severity": "warning", "reason": ""}])) \
        == ("degraded", ["GManDaemonSetMisscheduled"])
    assert workloads.classify(base(progressing=False, progressing_reason="ProgressDeadlineExceeded"))[0] == "stuck"
    assert workloads.classify(base(desired=None)) == ("unknown", ["нет метрик kube-state-metrics"])
    assert workloads.classify(base(desired=0, available=0, updated=0)) == ("ok", [])
    sts = dict(kind="StatefulSet", progressing=None, progressing_reason="")
    assert workloads.classify(base(**sts, updated=1, current_revision="a", update_revision="b"))[0] == "rolling"
    # Same revision: no rollout, whatever the updated count says.
    assert workloads.classify(base(**sts, updated=1, current_revision="a", update_revision="a"))[0] == "ok"
    ds = dict(kind="DaemonSet", progressing=None, progressing_reason="")
    assert workloads.classify(base(**ds, updated=2, unavailable=1, available=2))[0] == "rolling"
    assert workloads.classify(base(**ds, unavailable=1, available=2))[0] == "degraded"
    job = dict(kind="Job", desired=None, failed=False, active=1)
    assert workloads.classify(base(**job)) == ("ok", ["выполняется, pod-ов — 1"])
    assert workloads.classify(base(**{**job, "failed": True}))[0] == "degraded"
