"""Kubernetes node Events (kubelet, node-problem-detector) and Docker OOM without --watch: agent, API, /metrics."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from agent.gman_agent import cli, docker, kubernetes as k8s
from agent.gman_agent.watch import KubeWatcher
from backend.tests.test_alerts import find, samples
from backend.tests.test_api import client, post  # noqa: F401  (pytest fixture)
from backend.tests.test_kube_watch import node_ev, pod

CID = "4f2a" + "0" * 56 + "beef"  # 64-hex runtime container ID
OTHER = "9" * 64
UID = "0b5d6a5e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def gpu_pod(uid=UID, node="node-1", cid=CID):
    value = pod(uid, name="trainer-1", service="trainer")
    value["spec"]["nodeName"] = node
    value["status"]["containerStatuses"][0]["containerID"] = "containerd://" + cid
    return value


def refs(*pods):
    return {p["metadata"]["uid"]: k8s.pod_ref(p) for p in pods}


# NPD kernel-monitor: the default rule matches the "Killed process" line (process name and pid are workload data).
NPD_KILLED = ("Killed process 2592 (stress) total-vm:2075848kB, anon-rss:1048628kB, file-rss:4kB, shmem-rss:0kB, "
              "UID:0 pgtables:2124kB oom_score_adj:999")
# Kernels since 4.19 also log the cgroup of the victim; a custom NPD rule may include this line.
NPD_MEMCG = ("oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=cri-containerd-" + CID + ".scope,"
             "mems_allowed=0,oom_memcg=/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod"
             + UID.replace("-", "_") + ".slice/cri-containerd-" + CID + ".scope,task_memcg=/kubepods.slice/"
             "kubepods-burstable.slice/kubepods-burstable-pod" + UID.replace("-", "_") + ".slice/cri-containerd-"
             + CID + ".scope,task=python3,pid=4242,uid=1000")
# kubelet oom_watcher: only system-wide OOMs (cgroup "/") are recorded, on the Node object.
KUBELET_SYSTEM_OOM = "System OOM encountered, victim process: java, pid: 31337"


# --- Agent: mapping -------------------------------------------------------------------------------

def test_npd_oom_without_identifiers_is_attributed_to_the_node_only():
    mapped = k8s.kube_node_event(node_ev("n1", "OOMKilling", NPD_KILLED, component="kernel-monitor"),
                                 refs(gpu_pod()))
    assert mapped == {"uid": "n1", "kind": "Node", "node": "node-1", "reason": "OOMKilling", "type": "Warning",
                      "count": 1, "first_at": "2026-10-11T10:00:00Z", "last_at": "2026-10-11T10:03:00Z",
                      "service_id": "", "environment": "unknown", "namespace": "", "pod": "", "container": "",
                      "instance_id": "", "instance_name": "", "component": "kernel-monitor", "detail": ""}
    assert "stress" not in json.dumps(mapped) and "2592" not in json.dumps(mapped)


def test_npd_oom_with_container_id_is_mapped_to_the_instance():
    mapped = k8s.kube_node_event(node_ev("n2", "OOMKilling", NPD_MEMCG, component="kernel-monitor"),
                                 refs(gpu_pod()))
    assert (mapped["service_id"], mapped["instance_id"], mapped["instance_name"], mapped["namespace"]) == (
        "trainer", UID + ":app", "trainer-1/app", "prod")
    assert mapped["detail"] == "memory cgroup limit"
    text = json.dumps(mapped)
    assert "python3" not in text and "4242" not in text and CID not in text


def test_npd_oom_with_pod_uid_only_is_mapped_to_the_pod():
    message = "Memory cgroup out of memory: task_memcg=/kubepods/burstable/pod" + UID + " killed"
    mapped = k8s.kube_node_event(node_ev("n3", "OOMKilling", message), refs(gpu_pod(cid=OTHER)))
    assert (mapped["pod"], mapped["container"], mapped["instance_id"], mapped["service_id"]) == (
        "trainer-1", "", "", "trainer")
    assert mapped["instance_name"] == "trainer-1"


def test_oom_naming_a_pod_of_another_node_is_not_mapped():
    mapped = k8s.kube_node_event(node_ev("n4", "OOMKilling", NPD_MEMCG), refs(gpu_pod(node="node-2")))
    assert mapped["pod"] == "" and mapped["service_id"] == ""


def test_kubelet_system_oom_is_node_level():
    mapped = k8s.kube_node_event(node_ev("n5", "SystemOOM", KUBELET_SYSTEM_OOM), refs(gpu_pod()))
    assert (mapped["reason"], mapped["detail"], mapped["pod"]) == ("SystemOOM", "node out of memory", "")
    assert "java" not in json.dumps(mapped) and "31337" not in json.dumps(mapped)


@pytest.mark.parametrize("reason,message,detail", [
    ("EvictionThresholdMet", "Attempting to reclaim ephemeral-storage", "reclaiming ephemeral-storage"),
    ("NodeHasDiskPressure", "Node node-1 status is now: NodeHasDiskPressure", ""),
    ("NodeHasInsufficientMemory", "Node node-1 status is now: NodeHasInsufficientMemory", ""),
    ("TaskHung", "INFO: task java:1234 blocked for more than 120 seconds.", "task blocked for more than 120 seconds"),
    ("KernelOops", "BUG: unable to handle kernel NULL pointer dereference at 0000000000000008 secret",
     "NULL pointer dereference"),
    ("Rebooted", "Node node-1 has been rebooted, boot id: 7d6c1c1e-1111-2222-3333-444455556666", ""),
    ("OOMKilling", "Out of memory: Killed process 1 (init)", "node out of memory"),
])
def test_node_event_detail_keeps_only_fixed_phrases(reason, message, detail):
    assert k8s.node_event_detail(reason, message) == detail


def test_node_events_filtered_by_kind_reason_and_name():
    events = [node_ev("n1", "Rebooted", last="2026-10-11T10:01:00Z"),
              node_ev("n2", "NodeReady"),  # not allowlisted
              node_ev("n3", "Starting"),
              node_ev("n4", "TaskHung", node="Bad_Name"),
              {**node_ev("n5", "NodeNotReady", component="node-controller"), "type": "Normal",
               "lastTimestamp": None, "eventTime": "2026-10-11T10:04:00.5Z"},
              {**node_ev("n6", "SystemOOM"), "involvedObject": {"kind": "Pod", "name": "x", "uid": "p"}}]
    mapped = k8s.kube_events(events, {})
    assert [(m["uid"], m["type"], m["kind"]) for m in mapped] == [("n5", "Normal", "Node"), ("n1", "Warning", "Node")]


def test_kube_watcher_takes_node_events_from_their_own_feed():
    w = KubeWatcher(0, debounce=2, max_delay=10, interval=60)
    w.handle({"resource": "pods", "type": "SYNC", "items": [gpu_pod()]}, 0)
    w.take(0)
    w.handle({"resource": "node-events", "type": "ADDED", "object": node_ev("x", "Starting")}, 1)
    assert w.deadline() == 60
    w.handle({"resource": "node-events", "type": "ADDED",
              "object": node_ev("n1", "OOMKilling", NPD_MEMCG, component="kernel-monitor")}, 1)
    assert w.deadline() == 3
    data = {"observed_at": datetime.now(timezone.utc).isoformat(), "instances": []}
    [snapshot] = w.snapshots(data, w.take(3))
    assert [(e["uid"], e["instance_id"]) for e in snapshot["events"]] == [("n1", UID + ":app")]


# --- Agent: Docker OOM without --watch ------------------------------------------------------------

def oom_line(cid, seconds):
    return json.dumps({"Type": "container", "Action": "oom", "Actor": {"ID": cid, "Attributes": {}},
                       "time": seconds, "timeNano": seconds * 10 ** 9})


def docker_snapshot():
    return {"errors": [], "instances": [{"id": "c1", "last_oom_at": None}, {"id": "c2", "last_oom_at": None}]}


def test_one_shot_docker_reads_oom_events_since_the_previous_run(tmp_path):
    mark, calls = tmp_path / "docker-oom-server-01.since", []

    def run(args):
        calls.append(args)
        return "\n".join([oom_line("c1", 1_000_000_100), oom_line("c1", 1_000_000_200), "garbage",
                          oom_line("gone", 1_000_000_150)])

    data = docker.with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_300.7, run=run)
    assert calls[0][:6] == ["docker", "events", "--since", str(1_000_000_300 - docker.OOM_LOOKBACK),
                            "--until", "1000000300"]
    assert "event=oom" in calls[0]
    c1, c2 = data["instances"]
    assert datetime.fromisoformat(c1["last_oom_at"]) == datetime.fromtimestamp(1_000_000_200, timezone.utc)
    assert c2["last_oom_at"] is None and not data["errors"]
    assert mark.read_text() == "1000000300"
    # The next run continues from the previous one, never further back than the look-back.
    docker.with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_360, run=run)
    assert calls[1][2:6] == ["--since", "1000000300", "--until", "1000000360"]
    # Within the same second nothing is asked.
    docker.with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_360.9, run=run)
    assert len(calls) == 2


def test_one_shot_docker_oom_failure_keeps_the_mark(tmp_path):
    mark = tmp_path / "mark"
    mark.write_text("1000000000")

    def run(args):
        raise subprocess.CalledProcessError(1, args)

    data = docker.with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_060, run=run)
    assert data["errors"] == ["docker oom events unavailable: CalledProcessError"]
    assert mark.read_text() == "1000000000"


def test_one_shot_docker_send_uses_the_oom_mark(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("GMAN_URL", "https://gman.example")
    monkeypatch.setattr(cli, "collect", lambda *a: {"errors": [], "instances": []})
    monkeypatch.setattr(docker, "with_docker_ooms", lambda data, mark: seen.append(mark) or data)
    monkeypatch.setattr(cli, "run_once", lambda snapshot, spool, deliver: snapshot())
    cli.main(["--runtime", "docker", "--source-id", "server-01", "--send", "--state-dir", str(tmp_path)])
    assert seen == [tmp_path / "docker-oom-server-01.since"]


# --- API --------------------------------------------------------------------------------------------

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


def history(client, service_id=None):  # noqa: F811
    query = f"?service_id={service_id}" if service_id is not None else ""
    return [e for e in client.get("/api/events" + query).json()["events"]
            if e["type"] == "runtime-event"]


def test_node_events_are_recorded_once_and_mapped_ones_reach_the_service(client):  # noqa: F811
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


def test_node_event_validation(client):  # noqa: F811
    assert post(client, cluster(0, [node_event(reason="NodeReady")])).status_code == 422
    assert post(client, cluster(0, [node_event(reason="BackOff")])).status_code == 422  # a pod reason
    assert post(client, cluster(0, [node_event(node="")])).status_code == 422
    assert post(client, cluster(0, [node_event(node='gpu-1"} 1\nevil{x="')])).status_code == 422
    assert post(client, cluster(0, [node_event(message="free text")])).status_code == 422
    # SystemOOM is a node reason only.
    assert post(client, cluster(0, [{**node_event(reason="SystemOOM"), "kind": "Pod"}])).status_code == 422
    assert post(client, cluster(0, [node_event(reason="SystemOOM")])).json()["accepted"]


def test_node_events_on_metrics_and_node_card(client):  # noqa: F811
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


def test_old_node_events_leave_the_card_but_keep_the_metric(client):  # noqa: F811
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    post(client, cluster(0, [{**node_event(), "first_at": old, "last_at": old}]))
    assert client.get("/api/overview").json()["sources"][0]["node_events"] == []
    assert "gman_k8s_node_event_last_timestamp_seconds{" in client.get("/metrics").text


def test_pod_events_do_not_become_node_metrics(client):  # noqa: F811
    pod_event = {"uid": "e1", "reason": "OOMKilling", "count": 1, "last_at": at(60), "service_id": "api"}
    assert post(client, cluster(0, [pod_event])).json()["accepted"]
    assert "gman_k8s_node_event_last_timestamp_seconds{" not in client.get("/metrics").text
    assert history(client)[0]["kind"] == "Pod"


def test_rbac_lets_the_agent_list_and_watch_events_cluster_wide():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "deploy/kubernetes-rbac.yaml").read_text()
    documents = text.split("\n---\n")
    role = next(d for d in documents if "kind: ClusterRole\n" in d)
    assert "resources: ['pods', 'events']\n    verbs: ['list', 'watch']" in role
    binding = next(d for d in documents if "kind: ClusterRoleBinding" in d)
    assert "name: gman-inventory" in binding  # cluster-wide: node Events are in the namespace default
