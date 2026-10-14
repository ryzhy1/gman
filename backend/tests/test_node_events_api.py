"""Events узлов Kubernetes (kubelet, node-problem-detector) в API: история, /metrics и карточка узла."""
from datetime import datetime, timedelta, timezone

from metric_text import find, samples
from snapshot_factory import post

T0 = datetime.now(timezone.utc) - timedelta(hours=2)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def cluster(seconds, events=(), pressure=()):
    return {"source_id": "node-1", "source_type": "kubernetes-cluster", "hostname": "cluster", "observed_at": at(seconds),
            "nodes": [{"name": "gpu-1", "ready": True, "pressure": list(pressure)}], "instances": [],
            "events": list(events)}


def node_event(uid="n1", reason="OOMKilling", count=1, last=60, node="gpu-1", **extra):
    return {"uid": uid, "kind": "Node", "node": node, "reason": reason, "type": "Warning", "count": count,
            "first_at": at(0), "last_at": at(last), "component": "kernel-monitor", **extra}


def history(client, service_id=None):
    query = f"?service_id={service_id}" if service_id is not None else ""
    return [e for e in client.get("/api/events" + query).json()["events"]
            if e["type"] == "runtime-event"]


def test_node_events_are_recorded_once_and_mapped_ones_reach_the_service(client):
    mapped = node_event("n2", count=2, last=90, service_id="trainer", environment="prod", namespace="prod",
                        pod="trainer-1", container="app", instance_id="p1:app", instance_name="trainer-1/app",
                        detail="memory cgroup limit")
    assert post(client, cluster(0, [node_event(), mapped])).json()["accepted"]
    post(client, cluster(30, [node_event(), mapped]))  # relist: nothing new
    post(client, cluster(60, [node_event(count=3, last=120)]))
    recorded = history(client)
    assert [(e["uid"], e["occurrences"], e["service_id"]) for e in recorded] == [
        ("n1", 2, ""), ("n2", 2, "trainer"), ("n1", 1, "")]
    assert recorded[0]["node"] == "gpu-1" and recorded[0]["kind"] == "Node"
    assert [e["uid"] for e in history(client, "trainer")] == ["n2"]


def test_node_event_validation(client):
    assert post(client, cluster(0, [node_event(reason="NodeReady")])).status_code == 422
    assert post(client, cluster(0, [node_event(reason="BackOff")])).status_code == 422  # a pod reason
    assert post(client, cluster(0, [node_event(node="")])).status_code == 422
    assert post(client, cluster(0, [node_event(node='gpu-1"} 1\nevil{x="')])).status_code == 422
    assert post(client, cluster(0, [node_event(message="free text")])).status_code == 422
    # SystemOOM is a node reason only.
    assert post(client, cluster(0, [{**node_event(reason="SystemOOM"), "kind": "Pod"}])).status_code == 422
    assert post(client, cluster(0, [node_event(reason="SystemOOM")])).json()["accepted"]


def test_node_events_on_metrics_and_node_card(client):
    post(client, cluster(0, [node_event(), node_event("n2", last=200), node_event("n3", "EvictionThresholdMet", 4),
                             node_event("n4", "Rebooted", node="gpu-2")], pressure=["DiskPressure"]))
    data = samples(client.get("/metrics").text)
    oom = find(data, "gman_k8s_node_event_last_timestamp_seconds", source_id="node-1", node="gpu-1",
               reason="OOMKilling")
    assert oom == datetime.fromisoformat(at(200)).timestamp()
    assert find(data, "gman_k8s_node_event_last_timestamp_seconds", node="gpu-2", reason="Rebooted", host_id="gpu-2")
    assert find(data, "gman_k8s_node_pressure", host_id="gpu-1", condition="DiskPressure") == 1
    assert not [k for k in data if k[0] == "gman_k8s_node_event_last_timestamp_seconds" and ("reason", "BackOff") in k[1]]
    source = client.get("/api/overview").json()["sources"][0]
    summary = {(e["node"], e["reason"]): (e["count"], e["events"]) for e in source["node_events"]}
    assert summary == {("gpu-1", "OOMKilling"): (2, 2), ("gpu-1", "EvictionThresholdMet"): (4, 1),
                       ("gpu-2", "Rebooted"): (1, 1)}
    assert source["node_events"][0]["reason"] == "OOMKilling"  # newest first


def test_old_node_events_leave_the_card_but_keep_the_metric(client):
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    post(client, cluster(0, [{**node_event(), "first_at": old, "last_at": old}]))
    assert client.get("/api/overview").json()["sources"][0]["node_events"] == []
    assert "gman_k8s_node_event_last_timestamp_seconds{" in client.get("/metrics").text


def test_pod_events_do_not_become_node_metrics(client):
    pod_event = {"uid": "e1", "reason": "OOMKilling", "count": 1, "last_at": at(60), "service_id": "api"}
    assert post(client, cluster(0, [pod_event])).json()["accepted"]
    assert "gman_k8s_node_event_last_timestamp_seconds{" not in client.get("/metrics").text
    assert history(client)[0]["kind"] == "Pod"
