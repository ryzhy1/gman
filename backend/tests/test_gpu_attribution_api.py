"""Снимки GPU-узлов Kubernetes в API и метрики GPU, выделенных контейнерам Kubernetes."""
from datetime import datetime, timezone

import httpx

from telemetry.services.promql_templates import SCOPES

UID = "6515dd0c-1f3d-45c4-a644-84204c46d898"
CID = "4c3a" + "0" * 56 + "beef"


def node_snapshot(**extra):
    return {"source_id": "node-1.gpu-1", "source_type": "kubernetes-node", "hostname": "gpu-1", "cluster": "node-1",
            "observed_at": datetime.now(timezone.utc).isoformat(), "instances": [],
            "gpus": [{"id": "GPU-1", "vendor": "NVIDIA", "processes": [
                {"pid": 100, "container_id": CID, "used_memory_bytes": 1024, "pod_uid": UID, "namespace": "ml",
                 "pod": "trainer-0", "container": "trainer", "instance_id": UID + ":trainer"}]}], **extra}


def test_api_accepts_node_gpu_snapshot(client):
    assert client.post("/api/inventory", json=node_snapshot()).status_code == 200
    source = next(s for s in client.get("/api/overview").json()["sources"]
                  if s["source_id"] == "node-1.gpu-1")
    assert source["cluster"] == "node-1" and source["instances"] == []
    assert source["gpus"][0]["processes"][0]["instance_id"] == UID + ":trainer"
    instance = {"id": UID + ":trainer", "name": "trainer-0/trainer", "runtime": "kubernetes", "status": "running",
                "config": {"image": "trainer:v1"}}
    # A node source never carries instances (they belong to the cluster source) and must name its cluster.
    for bad in (node_snapshot(instances=[instance]), node_snapshot(cluster=""), node_snapshot(cluster="a b")):
        assert client.post("/api/inventory", json=bad).status_code == 422


def test_node_source_describes_only_the_node_it_is_named_after(client):
    # "node-1.gpu-1" cannot report another node or put the node into another cluster.
    for bad in (node_snapshot(hostname="gpu-2"), node_snapshot(cluster="other"),
                node_snapshot(cluster="node-1.gpu", hostname="1")):
        assert client.post("/api/inventory", json=bad).status_code == 422


def test_k8s_gpu_metrics_by_pod(configured):
    seen = []
    def handler(request):
        query = request.url.params["query"]
        seen.append(query)
        if "DCGM_FI_DEV_GPU_UTIL" in query and "namespace" in query:
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"namespace": "ml", "pod": "trainer-0", "container": "trainer"}, "value": [0, "87"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
    client = configured(handler)
    data = client.get("/api/metrics/current").json()
    assert data["k8s_gpu"] == {"ml/trainer-0/trainer": {"gpu_utilization_percent": 87.0}}
    seen.clear()
    params = {"scope": "k8s_gpu", "metric": "gpu_memory_used_bytes", "namespace": "ml", "pod_name": "trainer-0",
              "container_name": "trainer"}
    assert client.get("/api/metrics/range", params=params).status_code == 200
    selector = seen[0][seen[0].index("{") + 1:seen[0].index("}")].split(",")
    assert sorted(selector) == sorted(['pod!=""', 'namespace="ml"', 'pod="trainer-0"', 'container="trainer"'])
    for bad in ({k: v for k, v in params.items() if k != "pod_name"}, {**params, "pod_uid": UID},
                {**params, "scope": "k8s_container"}):
        assert client.get("/api/metrics/range", params=bad).status_code == 422
    assert set(SCOPES["k8s_gpu"].key_labels) == {"namespace", "pod", "container"}
