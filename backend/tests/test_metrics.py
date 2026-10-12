import httpx
import pytest

from backend.app import metrics
from backend.tests.test_api import auth, client  # noqa: F401  (fixture)


def prometheus(handler):
    metrics.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=1)


def vector(*items):
    return {"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": labels, "value": [0, value]} for labels, value in items]}}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom:9090/")
    yield
    metrics.client = httpx.Client(timeout=10)


def test_disabled_without_prometheus(client, monkeypatch):
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    assert client.get("/api/metrics/current", headers=auth("read")).json() == {
        "enabled": False, "error": "PROMETHEUS_URL is not configured"}
    assert client.get("/api/metrics/current").status_code == 401


def test_current_groups_by_object(client, configured):
    def handler(request):
        query = request.url.params["query"]
        if "container_cpu_usage" in query:
            return httpx.Response(200, json=vector(({"host_id": "h", "container_id": "c1"}, "0.5")))
        if "gman:gpu_temperature_celsius:max" in query:
            return httpx.Response(200, json=vector(({"host_id": "h", "UUID": "GPU-1"}, "61")))
        if "system_cpu_utilization_ratio" in query and "idle" in query:
            return httpx.Response(200, json=vector(({"host_id": "h"}, "NaN")))
        return httpx.Response(200, json=vector())
    prometheus(handler)
    data = client.get("/api/metrics/current", headers=auth("read")).json()
    assert data["error"] is None
    assert data["container"] == {"h/c1": {"cpu_cores": 0.5}}
    assert data["gpu"] == {"h/GPU-1": {"temperature_c": 61.0}}
    assert data["host"] == {"h": {"cpu_utilization": None}}


def test_unavailable_store_is_reported(client, configured):
    def handler(request):
        raise httpx.ConnectError("refused")
    prometheus(handler)
    data = client.get("/api/metrics/current", headers=auth("read")).json()
    assert data["enabled"] and data["error"].startswith("Metrics store unavailable")
    params = {"scope": "host", "metric": "load_1m", "host_id": "h"}
    assert client.get("/api/metrics/range", headers=auth("read"), params=params).status_code == 503


def test_range_uses_fixed_templates_and_escapes(client, configured):
    seen = []
    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {}, "values": [[1, "0.25"], [2, "0.5"]]}]}})
    prometheus(handler)
    params = {"scope": "container", "metric": "cpu_cores", "range": "24h", "host_id": "h", "container_id": "c1"}
    data = client.get("/api/metrics/range", headers=auth("read"), params=params).json()
    assert data["points"] == [[1.0, 0.25], [2.0, 0.5]] and data["step"] == 360
    assert 'host_id="h",container_id="c1"' in seen[0]["query"]
    bad = [{**params, "metric": "up"}, {**params, "range": "5y"}, {**params, "container_id": 'x"} or vector(1)'},
           {k: v for k, v in params.items() if k != "container_id"}]
    for value in bad:
        assert client.get("/api/metrics/range", headers=auth("read"), params=value).status_code == 422
    assert len(seen) == 1
    assert metrics.selector({"host_id": 'a"b\\'})() == '{host_id="a\\"b\\\\"}'


POD = "6515dd0c-1f3d-45c4-a644-84204c46d898"


def test_current_keys_kubernetes_objects_like_inventory(client, configured):
    def handler(request):
        query = request.url.params["query"]
        if "container_cpu_time_seconds_total" in query:
            return httpx.Response(200, json=vector(({"k8s_pod_uid": POD, "k8s_container_name": "api"}, "0.25")))
        if "container_memory_working_set_bytes" in query:
            return httpx.Response(200, json=vector(({"k8s_pod_uid": POD, "k8s_container_name": "api"}, "1048576")))
        if "k8s_pod_network_io_bytes_total" in query and '"receive"' in query:
            return httpx.Response(200, json=vector(({"k8s_pod_uid": POD}, "+Inf")))
        return httpx.Response(200, json=vector())
    prometheus(handler)
    data = client.get("/api/metrics/current", headers=auth("read")).json()
    assert data["k8s_container"] == {f"{POD}:api": {"cpu_cores": 0.25, "memory_bytes": 1048576.0}}
    assert data["k8s_pod"] == {POD: {"network_rx_bytes_per_second": None}}
    assert data["container"] == {} and data["host"] == {} and data["gpu"] == {}


def test_range_kubernetes_scopes(client, configured):
    seen = []
    def handler(request):
        seen.append(request.url.params["query"])
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})
    prometheus(handler)
    params = {"scope": "k8s_container", "metric": "cpu_cores", "pod_uid": POD, "container_name": "api"}
    data = client.get("/api/metrics/range", headers=auth("read"), params=params).json()
    assert data["points"] == [] and data["scope"] == "k8s_container" and data["range"] == "1h"
    assert f'k8s_pod_uid!="",k8s_pod_uid="{POD}",k8s_container_name="api"' in seen[0]
    assert seen[0].startswith("max by (k8s_pod_uid, k8s_container_name) (rate(container_cpu_time_seconds_total{")
    pod = {"scope": "k8s_pod", "metric": "network_tx_bytes_per_second", "pod_uid": POD}
    assert client.get("/api/metrics/range", headers=auth("read"), params=pod).status_code == 200
    assert f'{{direction="transmit",k8s_pod_uid="{POD}"}}' in seen[1]
    bad = [{**params, "container_name": 'api"} or vector(1)'}, {**params, "pod_uid": "a b"},
           {k: v for k, v in params.items() if k != "container_name"}, {**params, "host_id": "h"},
           {**params, "metric": "network_rx_bytes_per_second"}, {**pod, "container_name": "api"},
           {**pod, "metric": "cpu_cores"}, {"scope": "host", "metric": "load_1m"}, {**params, "scope": "k8s_node"}]
    for value in bad:
        assert client.get("/api/metrics/range", headers=auth("read"), params=value).status_code == 422, value
    assert len(seen) == 2


def test_llm_scope_by_service_and_model(client, configured):
    seen = []

    def handler(request):
        query = request.url.params["query"]
        seen.append(query)
        if request.url.path.endswith("/query") and "gman:llm_ttft_seconds:p95" in query:
            return httpx.Response(200, json=vector(({"gman_service": "chat", "model_name": "meta-llama/Llama-3.1-8B"}, "0.42")))
        if request.url.path.endswith("/query_range"):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})
        return httpx.Response(200, json=vector())
    prometheus(handler)
    data = client.get("/api/metrics/current", headers=auth("read")).json()
    assert data["llm"] == {"chat/meta-llama/Llama-3.1-8B": {"ttft_p95_seconds": 0.42}}
    params = {"scope": "llm", "metric": "ttft_p95_seconds", "service_id": "chat", "model": "meta-llama/Llama-3.1-8B"}
    assert client.get("/api/metrics/range", headers=auth("read"), params=params).status_code == 200
    assert 'gman_service="chat",model_name="meta-llama/Llama-3.1-8B"' in seen[-1]
    # The model alone does not identify an object.
    params.pop("service_id")
    assert client.get("/api/metrics/range", headers=auth("read"), params=params).status_code == 422
