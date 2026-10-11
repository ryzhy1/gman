import time

import httpx
import pytest

from backend.app import alerts, exposition
from backend.tests.test_api import auth, client, post, snapshot  # noqa: F401  (fixture)


def samples(text):
    """{(name, frozenset(labels)): value} from the text exposition format."""
    result = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, value = line.rsplit(" ", 1)
        name, _, rest = head.partition("{")
        labels = frozenset(tuple(pair.split("=", 1)) for pair in rest.rstrip("}").split('",') if pair)
        result[(name, frozenset((k, v.strip('"')) for k, v in labels))] = float(value)
    return result


def find(data, name, **labels):
    found = [v for (n, ls), v in data.items() if n == name and set(labels.items()) <= set(ls)]
    assert len(found) == 1, (name, labels, found)
    return found[0]


def cluster():
    value = snapshot()
    value.update(source_id="node-2", source_type="kubernetes-cluster",
                 nodes=[{"name": "k8s-a", "ready": True}, {"name": "k8s-b", "ready": False}])
    value["instances"] = [
        {"id": "uid-1:app", "name": "app-1/app", "runtime": "kubernetes", "node": "k8s-a", "namespace": "prod",
         "service_id": "api", "environment": "prod", "status": "waiting", "reason": "CrashLoopBackOff",
         "last_termination_reason": "OOMKilled exit 137", "restarts": 7, "config": {"image": "api:v1"}},
        {"id": "uid-2:web", "name": "web-1/web", "runtime": "kubernetes", "status": "waiting",
         "reason": "some free-form text", "config": {"image": "web:v1"}}]
    return value


def test_metrics_expose_inventory_state(client):
    host = snapshot()
    host["instances"][0].update(status="exited", reason="OOMKilled", restarts=2)
    host["errors"] = ["nvidia-smi: extended GPU fields unsupported by this driver"]
    host["gpus"] = [{"id": "GPU-1", "vendor": "NVIDIA", "model": "A100", "temperature_c": 71,
                     "ecc_uncorrected_errors": 0, "throttle_reasons": ["gpu_idle", "hw_thermal"]},
                    {"id": "card1", "vendor": "AMD", "temperature_c": 50}]
    assert post(client, host).json()["accepted"]
    assert client.post("/api/inventory", headers=auth("agent2"), json=cluster()).json()["accepted"]

    response = client.get("/metrics")  # Internal endpoint: no credential, not proxied by nginx.
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    data = samples(response.text)
    # Full precision: alerts compare it with time().
    assert abs(find(data, "gman_source_last_snapshot_timestamp_seconds", source_id="node-1", host_id="node-1")
               - time.time()) < 60
    assert not any(n == "gman_source_last_snapshot_timestamp_seconds" and ("host_id", "node-2") in ls
                   for n, ls in data)
    assert find(data, "gman_source_collect_errors", source_id="node-1") == 1
    assert find(data, "gman_k8s_node_ready", host_id="k8s-b") == 0
    assert find(data, "gman_gpu_temperature_celsius", host_id="node-1", UUID="GPU-1") == 71
    assert find(data, "gman_gpu_ecc_uncorrected_errors", UUID="GPU-1") == 0
    assert find(data, "gman_gpu_clock_event_reasons", UUID="GPU-1") == 0x41
    assert not any("card1" in str(ls) for _, ls in data)  # Only NVIDIA: labels follow DCGM.
    assert find(data, "gman_instance_restarts_total", instance_id="container-1", host_id="node-1") == 2
    assert find(data, "gman_instance_state_reason", instance_id="container-1", reason="OOMKilled") == 1
    assert find(data, "gman_instance_restarts_total", instance_id="uid-1:app", host_id="k8s-a") == 7
    assert find(data, "gman_instance_state_reason", instance_id="uid-1:app", reason="CrashLoopBackOff") == 1
    assert find(data, "gman_instance_last_termination_reason", instance_id="uid-1:app", reason="OOMKilled") == 1
    # Free-form reasons are not label values.
    assert not any(n == "gman_instance_state_reason" and ("instance_id", "uid-2:web") in ls for n, ls in data)


def test_stale_gpu_readings_are_dropped(client, monkeypatch):
    host = snapshot()
    host["gpus"] = [{"id": "GPU-1", "vendor": "NVIDIA", "temperature_c": 95}]
    post(client, host)
    monkeypatch.setattr(exposition, "GPU_FRESH_SECONDS", -1)
    text = client.get("/metrics").text
    assert "gman_gpu_temperature_celsius{" not in text
    assert 'gman_instance_restarts_total{source_id="node-1"' in text


def test_node_gpu_source_is_labelled_with_its_node(client, monkeypatch):
    """A kubernetes-node source "<cluster>.<node>" reports for the Kubernetes node (its hostname): the same host_id
    as DCGM and gman_k8s_node_ready, so DCGM replaces nvidia-smi in the recording rules rather than adding a second
    series for the same GPU, and GManK8sNodeNotReady inhibits the node source's GPU and GManSourceSilent alerts."""
    monkeypatch.setenv("GMAN_AGENT_TOKENS", '{"cluster-01.gpu-1":"agent-gpu-1"}')
    node = {"source_id": "cluster-01.gpu-1", "source_type": "kubernetes-node", "hostname": "gpu-1",
            "cluster": "cluster-01", "observed_at": snapshot()["observed_at"], "instances": [],
            "gpus": [{"id": "GPU-9", "vendor": "NVIDIA", "temperature_c": 80, "throttle_reasons": []}]}
    assert client.post("/api/inventory", headers=auth("agent-gpu-1"), json=node).json()["accepted"]
    data = samples(client.get("/metrics").text)
    assert find(data, "gman_gpu_temperature_celsius", UUID="GPU-9") == 80
    assert find(data, "gman_gpu_temperature_celsius", source_id="cluster-01.gpu-1", host_id="gpu-1") == 80
    assert find(data, "gman_source_last_snapshot_timestamp_seconds", source_id="cluster-01.gpu-1", host_id="gpu-1",
                source_type="kubernetes-node") > 0


def test_label_values_are_escaped():
    assert exposition.line("m", {"a": 'x"y\\z\nw'}, 1) == 'm{a="x\\"y\\\\z\\nw"} 1.0'


@pytest.fixture
def alertmanager(monkeypatch):
    monkeypatch.setenv("ALERTMANAGER_URL", "http://alertmanager:9093/")
    yield lambda handler: setattr(alerts, "client", httpx.Client(transport=httpx.MockTransport(handler), timeout=1))
    alerts.client = httpx.Client(timeout=5)


def test_alerts_disabled_without_alertmanager(client, monkeypatch):
    monkeypatch.delenv("ALERTMANAGER_URL", raising=False)
    assert client.get("/api/alerts").status_code == 401
    assert client.get("/api/alerts", headers=auth("read")).json() == {
        "enabled": False, "error": "ALERTMANAGER_URL is not configured", "alerts": []}


def test_active_alerts_are_read_from_alertmanager(client, alertmanager):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=[
            {"labels": {"alertname": "GManGpuOverheating", "severity": "warning", "host_id": "h", "UUID": "GPU-1",
                        "gman_portal": "https://gman.example"},
             "annotations": {"summary": "hot", "runbook_url": "https://gman.example/#alerts/GManGpuOverheating"},
             "startsAt": "2026-10-11T10:00:00Z"},
            {"labels": {"alertname": "GManSourceSilent", "severity": "critical", "source_id": "s"},
             "annotations": {"summary": "silent"}, "startsAt": "2026-10-11T11:00:00Z"}])
    alertmanager(handler)
    data = client.get("/api/alerts", headers=auth("read")).json()
    assert data["error"] is None
    assert [a["name"] for a in data["alerts"]] == ["GManSourceSilent", "GManGpuOverheating"]
    assert data["alerts"][1]["labels"] == {"severity": "warning", "host_id": "h", "UUID": "GPU-1"}
    assert data["alerts"][1]["runbook_url"].endswith("#alerts/GManGpuOverheating")
    assert seen[0].path == "/api/v2/alerts"
    assert dict(seen[0].params) == {"active": "true", "silenced": "false", "inhibited": "false"}


def test_unavailable_alertmanager_is_reported(client, alertmanager):
    def handler(request):
        raise httpx.ConnectError("refused")
    alertmanager(handler)
    data = client.get("/api/alerts", headers=auth("read")).json()
    assert data["enabled"] and data["alerts"] == [] and data["error"].startswith("Alertmanager unavailable")
