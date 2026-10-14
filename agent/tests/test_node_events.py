"""Events узлов Kubernetes (kubelet, node-problem-detector) и OOM Docker без --watch в агенте."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import collect
from collection.adapters.oom_mark_store import OomMarkStore
from collection.mappers.kubernetes_event_mapper import kube_events, kube_node_event, node_event_detail
from collection.mappers.kubernetes_pod_mapper import pod_ref
from collection.mappers.snapshot_mapper import instances_of
from collection.orchestrator import CollectionOrchestrator
from collection.services.docker_oom_history import OOM_LOOKBACK_SECONDS, DockerOomHistory
from conftest import FakeRunner, runtime, settings
from delivery.orchestrator import DeliveryOrchestrator
from runtime_access.docker_cli import DockerCli
from test_kube_watch import item, kube_watcher, node_ev, pod, snapshots

CID = "4f2a" + "0" * 56 + "beef"  # 64-hex runtime container ID
OTHER = "9" * 64
UID = "0b5d6a5e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def gpu_pod(uid=UID, node="node-1", cid=CID):
    value = pod(uid, name="trainer-1", service="trainer")
    value["spec"]["nodeName"] = node
    value["status"]["containerStatuses"][0]["containerID"] = "containerd://" + cid
    return value


def refs(*pods):
    return {p["metadata"]["uid"]: pod_ref(p) for p in pods}


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
    mapped = kube_node_event(node_ev("n1", "OOMKilling", NPD_KILLED, component="kernel-monitor"),
                                 refs(gpu_pod()))
    assert mapped == {"uid": "n1", "kind": "Node", "node": "node-1", "reason": "OOMKilling", "type": "Warning",
                      "count": 1, "first_at": "2026-10-11T10:00:00Z", "last_at": "2026-10-11T10:03:00Z",
                      "service_id": "", "environment": "unknown", "namespace": "", "pod": "", "container": "",
                      "instance_id": "", "instance_name": "", "component": "kernel-monitor", "detail": ""}
    assert "stress" not in json.dumps(mapped) and "2592" not in json.dumps(mapped)


def test_npd_oom_with_container_id_is_mapped_to_the_instance():
    mapped = kube_node_event(node_ev("n2", "OOMKilling", NPD_MEMCG, component="kernel-monitor"),
                                 refs(gpu_pod()))
    assert (mapped["service_id"], mapped["instance_id"], mapped["instance_name"], mapped["namespace"]) == (
        "trainer", UID + ":app", "trainer-1/app", "prod")
    assert mapped["detail"] == "memory cgroup limit"
    text = json.dumps(mapped)
    assert "python3" not in text and "4242" not in text and CID not in text


def test_npd_oom_with_pod_uid_only_is_mapped_to_the_pod():
    message = "Memory cgroup out of memory: task_memcg=/kubepods/burstable/pod" + UID + " killed"
    mapped = kube_node_event(node_ev("n3", "OOMKilling", message), refs(gpu_pod(cid=OTHER)))
    assert (mapped["pod"], mapped["container"], mapped["instance_id"], mapped["service_id"]) == (
        "trainer-1", "", "", "trainer")
    assert mapped["instance_name"] == "trainer-1"


def test_oom_naming_a_pod_of_another_node_is_not_mapped():
    mapped = kube_node_event(node_ev("n4", "OOMKilling", NPD_MEMCG), refs(gpu_pod(node="node-2")))
    assert mapped["pod"] == "" and mapped["service_id"] == ""


def test_kubelet_system_oom_is_node_level():
    mapped = kube_node_event(node_ev("n5", "SystemOOM", KUBELET_SYSTEM_OOM), refs(gpu_pod()))
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
    assert node_event_detail(reason, message) == detail


def test_node_events_filtered_by_kind_reason_and_name():
    events = [node_ev("n1", "Rebooted", last="2026-10-11T10:01:00Z"),
              node_ev("n2", "NodeReady"),  # not allowlisted
              node_ev("n3", "Starting"),
              node_ev("n4", "TaskHung", node="Bad_Name"),
              {**node_ev("n5", "NodeNotReady", component="node-controller"), "type": "Normal",
               "lastTimestamp": None, "eventTime": "2026-10-11T10:04:00.5Z"},
              {**node_ev("n6", "SystemOOM"), "involvedObject": {"kind": "Pod", "name": "x", "uid": "p"}}]
    mapped = kube_events(events, {})
    assert [(m["uid"], m["type"], m["kind"]) for m in mapped] == [("n5", "Normal", "Node"), ("n1", "Warning", "Node")]


def test_kube_watcher_takes_node_events_from_their_own_feed():
    w = kube_watcher(debounce=2, max_delay=10, interval=60)
    w.handle(item("pods", "SYNC", gpu_pod()), 0)
    w.take(0)
    w.handle(item("node-events", "ADDED", node_ev("x", "Starting")), 1)
    assert w.deadline() == 60
    w.handle(item("node-events", "ADDED", node_ev("n1", "OOMKilling", NPD_MEMCG, component="kernel-monitor")), 1)
    assert w.deadline() == 3
    data = {"observed_at": datetime.now(timezone.utc).isoformat(), "instances": []}
    [snapshot] = snapshots(w, data, w.take(3))
    assert [(e["uid"], e["instance_id"]) for e in snapshot["events"]] == [("n1", UID + ":app")]


# --- Agent: Docker OOM without --watch ------------------------------------------------------------

def oom_line(cid, seconds):
    return json.dumps({"Type": "container", "Action": "oom", "Actor": {"ID": cid, "Attributes": {}},
                       "time": seconds, "timeNano": seconds * 10 ** 9})


def docker_snapshot():
    return {"errors": [], "instances": [{"id": "c1", "last_oom_at": None}, {"id": "c2", "last_oom_at": None}]}


def with_docker_ooms(data, mark, clock, run):
    """Снимок после добавления OOM из событий демона (как запуск Docker по таймеру)."""
    runner = FakeRunner(run)
    DockerOomHistory(DockerCli(runner), OomMarkStore(), clock).apply(instances_of(data), data["errors"], mark)
    with_docker_ooms.calls = runner.calls
    return data


def test_one_shot_docker_reads_oom_events_since_the_previous_run(tmp_path):
    mark, calls = tmp_path / "docker-oom-server-01.since", []

    def run(args):
        calls.append(args)
        return "\n".join([oom_line("c1", 1_000_000_100), oom_line("c1", 1_000_000_200), "garbage",
                          oom_line("gone", 1_000_000_150)])

    data = with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_300.7, run=run)
    assert calls[0][:6] == ["docker", "events", "--since", str(1_000_000_300 - OOM_LOOKBACK_SECONDS),
                            "--until", "1000000300"]
    assert "event=oom" in calls[0]
    c1, c2 = data["instances"]
    assert datetime.fromisoformat(c1["last_oom_at"]) == datetime.fromtimestamp(1_000_000_200, timezone.utc)
    assert c2["last_oom_at"] is None and not data["errors"]
    assert mark.read_text() == "1000000300"
    # The next run continues from the previous one, never further back than the look-back.
    with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_360, run=run)
    assert calls[1][2:6] == ["--since", "1000000300", "--until", "1000000360"]
    # Within the same second nothing is asked.
    with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_360.9, run=run)
    assert len(calls) == 2


def test_one_shot_docker_oom_failure_keeps_the_mark(tmp_path):
    mark = tmp_path / "mark"
    mark.write_text("1000000000")

    def run(args):
        raise subprocess.CalledProcessError(1, args)

    data = with_docker_ooms(docker_snapshot(), mark, clock=lambda: 1_000_000_060, run=run)
    assert data["errors"] == ["docker oom events unavailable: CalledProcessError"]
    assert mark.read_text() == "1000000000"


def test_one_shot_docker_send_uses_the_oom_mark(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(CollectionOrchestrator, "_docker_snapshot", lambda self, errors: {"errors": [], "instances": []})
    monkeypatch.setattr(DockerOomHistory, "apply", lambda self, instances, errors, mark: seen.append(mark))
    monkeypatch.setattr(DeliveryOrchestrator, "run_once", lambda self, collect_snapshot: collect_snapshot())
    collect.main(["--runtime", "docker", "--source-id", "server-01", "--send", "--state-dir", str(tmp_path)],
                 settings(gman_url="https://gman.example"), runtime())
    assert seen == [tmp_path / "docker-oom-server-01.since"]


def test_rbac_lets_the_agent_list_and_watch_events_cluster_wide():
    text = (Path(__file__).resolve().parents[2] / "deploy/kubernetes-rbac.yaml").read_text()
    documents = text.split("\n---\n")
    role = next(d for d in documents if "kind: ClusterRole\n" in d)
    assert "resources: ['pods', 'events']\n    verbs: ['list', 'watch']" in role
    binding = next(d for d in documents if "kind: ClusterRoleBinding" in d)
    assert "name: gman-inventory" in binding  # cluster-wide: node Events are in the namespace default
