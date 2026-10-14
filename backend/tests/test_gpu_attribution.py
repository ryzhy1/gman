"""GPU process attribution on Kubernetes nodes: /proc/<pid>/cgroup -> container ID -> pod."""
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from agent.gman_agent import cli, common, gpu, kubernetes as k8s
from backend.app import metrics
from backend.tests.test_api import client  # noqa: F401  (fixture)
from backend.tests.test_metrics import configured, prometheus  # noqa: F401  (fixture)

UID = "6515dd0c-1f3d-45c4-a644-84204c46d898"
SYSTEMD_UID = UID.replace("-", "_")
CID = "4c3a" + "0" * 56 + "beef"
OTHER = "d" * 64

# (case, /proc/<pid>/cgroup contents, expected container ID, expected pod UID)
CGROUPS = [
    ("v1 cgroupfs containerd besteffort",
     f"12:pids:/kubepods/besteffort/pod{UID}/{CID}\n11:memory:/kubepods/besteffort/pod{UID}/{CID}\n"
     f"1:name=systemd:/kubepods/besteffort/pod{UID}/{CID}\n", CID, UID),
    ("v1 cgroupfs guaranteed (no QoS level)",
     f"4:cpu,cpuacct:/kubepods/pod{UID}/{CID}\n", CID, UID),
    ("v1 systemd containerd burstable",
     f"11:memory:/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{SYSTEMD_UID}.slice/"
     f"cri-containerd-{CID}.scope\n", CID, UID),
    ("v1 hybrid: unrelated hierarchy first",
     f"13:rdma:/\n12:pids:/user.slice\n0::/kubepods.slice/kubepods-besteffort.slice/"
     f"kubepods-besteffort-pod{SYSTEMD_UID}.slice/cri-containerd-{CID}.scope\n", CID, UID),
    ("v2 systemd containerd besteffort",
     f"0::/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod{SYSTEMD_UID}.slice/"
     f"cri-containerd-{CID}.scope\n", CID, UID),
    ("v2 systemd guaranteed",
     f"0::/kubepods.slice/kubepods-pod{SYSTEMD_UID}.slice/cri-containerd-{CID}.scope\n", CID, UID),
    ("v2 systemd CRI-O",
     f"0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{SYSTEMD_UID}.slice/crio-{CID}.scope\n", CID, UID),
    ("v2 systemd CRI-O with nested container cgroup",
     f"0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{SYSTEMD_UID}.slice/crio-{CID}.scope/container\n",
     CID, UID),
    ("v2 CRI-O conmon is not the container",
     f"0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{SYSTEMD_UID}.slice/crio-conmon-{CID}.scope\n",
     "", UID),
    ("v2 cgroupfs containerd",
     f"0::/kubepods/burstable/pod{UID}/{CID}\n", CID, UID),
    ("v2 dockershim / cri-dockerd",
     f"0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{SYSTEMD_UID}.slice/docker-{CID}.scope\n",
     CID, UID),
    ("v2 read from a private cgroup namespace (relative path)",
     f"0::/../../kubepods-besteffort.slice/kubepods-besteffort-pod{SYSTEMD_UID}.slice/cri-containerd-{CID}.scope\n",
     CID, UID),
    ("kind / nested kubelet slice",
     f"0::/kubelet.slice/kubelet-kubepods.slice/kubelet-kubepods-besteffort.slice/"
     f"kubelet-kubepods-besteffort-pod{SYSTEMD_UID}.slice/cri-containerd-{CID}.scope\n", CID, UID),
    ("static pod: config hash instead of UID",
     f"0::/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod{'0f' * 16}.slice/cri-containerd-{CID}.scope\n",
     CID, "0f" * 16),
    ("systemd slice:prefix:id form (mismatched cgroup drivers)",
     f"0::/system.slice/containerd.service/kubepods-burstable-pod{SYSTEMD_UID}.slice:cri-containerd:{CID}\n", CID, UID),
    ("v1 docker without Kubernetes", f"11:memory:/docker/{CID}\n", CID, ""),
    ("v2 docker without Kubernetes", f"0::/system.slice/docker-{CID}.scope\n", CID, ""),
    ("v2 podman", f"0::/machine.slice/libpod-{CID}.scope/container\n", CID, ""),
    ("host process", "0::/user.slice/user-1000.slice/session-3.scope\n", "", ""),
    ("systemd service", "0::/system.slice/nvidia-persistenced.service\n", "", ""),
    ("empty", "", "", ""),
]


@pytest.mark.parametrize("case,text,container,pod", CGROUPS, ids=[c[0] for c in CGROUPS])
def test_parse_cgroup_variants(case, text, container, pod):
    assert gpu.parse_cgroup(text) == {"container_id": container, "pod_uid": pod}


def pod_json(uid, name, namespace, statuses, annotations=None):
    return {"metadata": {"uid": uid, "name": name, "namespace": namespace, "annotations": annotations or {}},
            "spec": {"nodeName": "gpu-1"}, "status": {"containerStatuses": statuses}}


STATIC_HASH = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"  # Static pod: cgroup UID is the config hash.
PODS = {"items": [
    pod_json(UID, "trainer-0", "ml", [{"name": "trainer", "containerID": "containerd://" + CID},
                                      {"name": "sidecar", "containerID": "containerd://" + "e" * 64}]),
    pod_json("11111111-2222-3333-4444-555555555555", "infer-x", "serving",
             [{"name": "server", "containerID": "cri-o://" + OTHER}]),
    pod_json("99999999-8888-7777-6666-555555555555", "etcd-gpu-1", "kube-system",
             [{"name": "etcd", "containerID": "containerd://" + "a" * 64}], {"kubernetes.io/config.hash": STATIC_HASH}),
]}
CGROUP_BY_PID = {
    # Containerd, systemd driver, cgroup v2.
    "100": f"0::/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod{SYSTEMD_UID}.slice/cri-containerd-{CID}.scope\n",
    # CRI-O, cgroupfs driver, cgroup v1.
    "200": f"11:memory:/kubepods/burstable/pod11111111-2222-3333-4444-555555555555/{OTHER}\n",
    # Container not (yet) in containerStatuses: pod-level attribution from the cgroup UID.
    "300": f"0::/kubepods/burstable/pod{UID}/" + "f" * 64 + "\n",
    # Static pod: the cgroup carries the config hash, matched through the annotation.
    "400": f"0::/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod{STATIC_HASH}.slice/"
           "cri-containerd-" + "b" * 64 + ".scope\n",
    "500": "0::/user.slice/user-1000.slice/session-3.scope\n",
}
QUERY = "GPU-1, NVIDIA A100-SXM4-80GB, 00000000:3B:00.0, 550.54.15, P0, 37, 12, 81920, 40960, 61, 251.50"
APPS = "GPU-1, 100, 1024\nGPU-1, 200, 2048\nGPU-1, 300, 10\nGPU-1, 400, 20\nGPU-1, 500, 30\nGPU-1, 600, 40\n"


@pytest.fixture
def node(monkeypatch, tmp_path):
    """Fixture /proc with cgroup files; mocked nvidia-smi (base fields only) and kubectl."""
    for pid, text in CGROUP_BY_PID.items():
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cgroup").write_text(text)
    monkeypatch.setattr(gpu, "PROC_ROOT", str(tmp_path))  # PID 600 exited: no /proc entry.
    calls = []
    def fake(args):
        calls.append(args)
        if args[0] == "kubectl":
            return json.dumps(PODS)
        if "clocks_throttle_reasons.active" in args[1]:
            raise subprocess.CalledProcessError(2, args)
        return QUERY + "\n" if args[1].startswith("--query-gpu") else APPS
    monkeypatch.setattr(common, "command", fake)
    return calls


def test_node_snapshot_attributes_processes_to_pods(node):
    data = cli.collect("kubernetes-node", "cluster-01.gpu-1", "gpu-1", "cluster-01")
    assert ["kubectl", "get", "pods", "--all-namespaces", "--field-selector", "spec.nodeName=gpu-1", "-o", "json"] in node
    assert (data["source_type"], data["hostname"], data["cluster"], data["instances"]) == (
        "kubernetes-node", "gpu-1", "cluster-01", [])
    processes = {p["pid"]: p for p in data["gpus"][0]["processes"]}
    assert processes[100] == {"pid": 100, "container_id": CID, "used_memory_bytes": 1024**3, "pod_uid": UID,
                              "namespace": "ml", "pod": "trainer-0", "container": "trainer",
                              "instance_id": UID + ":trainer"}
    assert processes[200]["instance_id"] == "11111111-2222-3333-4444-555555555555:server"
    assert (processes[200]["namespace"], processes[200]["pod"]) == ("serving", "infer-x")
    # Known pod, unknown container: pod only, no instance to link to.
    assert (processes[300]["pod"], processes[300].get("instance_id")) == ("trainer-0", None)
    assert processes[400]["pod"] == "etcd-gpu-1" and "instance_id" not in processes[400]
    assert processes[500] == {"pid": 500, "container_id": "", "used_memory_bytes": 30 * 1024**2}
    assert processes[600]["container_id"] == ""
    from backend.app.main import Snapshot
    Snapshot(**data)


def test_node_snapshot_without_pod_list_keeps_cgroup_ids(node, monkeypatch):
    real = common.command
    def no_kubectl(args):
        if args[0] == "kubectl":
            raise FileNotFoundError("kubectl")
        return real(args)
    monkeypatch.setattr(common, "command", no_kubectl)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    data = cli.collect("kubernetes-node", "cluster-01.gpu-1", "gpu-1", "cluster-01")
    assert "pods of the node unavailable: FileNotFoundError" in data["errors"]
    process = data["gpus"][0]["processes"][0]
    assert (process["container_id"], process["pod_uid"], "pod" in process) == (CID, UID, False)


def test_kube_get_falls_back_to_in_cluster_api(monkeypatch, tmp_path):
    (tmp_path / "token").write_text("sa-token\n")
    (tmp_path / "ca.crt").write_text("")
    monkeypatch.setattr(k8s, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "6443")
    monkeypatch.setattr(common, "command", lambda args: (_ for _ in ()).throw(FileNotFoundError(args[0])))
    monkeypatch.setattr(k8s.ssl, "create_default_context", lambda cafile: ("context", cafile))
    seen = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def read(self):
            return json.dumps(PODS).encode()

    def fake_urlopen(request, timeout, context):
        seen.update(url=request.full_url, auth=request.get_header("Authorization"), context=context)
        return Response()
    monkeypatch.setattr(k8s, "urlopen", fake_urlopen)
    assert len(k8s.node_pods("gpu-1")) == 3
    assert seen["url"] == "https://10.0.0.1:6443/api/v1/pods?fieldSelector=spec.nodeName%3Dgpu-1"
    assert seen["auth"] == "Bearer sa-token" and seen["context"] == ("context", str(tmp_path / "ca.crt"))


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


def test_k8s_gpu_metrics_by_pod(client, configured):
    seen = []
    def handler(request):
        query = request.url.params["query"]
        seen.append(query)
        if "DCGM_FI_DEV_GPU_UTIL" in query and "namespace" in query:
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"namespace": "ml", "pod": "trainer-0", "container": "trainer"}, "value": [0, "87"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
    prometheus(handler)
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
    assert set(metrics.SCOPES["k8s_gpu"][1]) == {"namespace", "pod", "container"}


def test_systemd_unit_runs_node_mode_from_environment(tmp_path, monkeypatch):
    """deploy/systemd/gman-collect.service passes only --runtime/--source-id/--send: the node name and the
    cluster of the kubernetes-node mode must come from the environment file (NODE_NAME, GMAN_CLUSTER)."""
    unit = (Path(__file__).resolve().parents[2] / "deploy/systemd/gman-collect.service").read_text()
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    env = {"GMAN_RUNTIME": "kubernetes-node", "GMAN_SOURCE_ID": "cluster-01.gpu-1", "NODE_NAME": "gpu-1",
           "GMAN_CLUSTER": "cluster-01", "GMAN_URL": "https://gman.example",
           "GMAN_AGENT_STATE_DIR": str(tmp_path)}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    argv = [os.path.expandvars(a) for a in shlex.split(exec_start.split("=", 1)[1])[2:]]
    seen = []

    def fake(*args):
        seen.append(args)
        raise RuntimeError("stop before sending")

    monkeypatch.setattr(cli, "collect", fake)
    with pytest.raises(RuntimeError, match="stop"):
        cli.main(argv)
    assert seen == [("kubernetes-node", "cluster-01.gpu-1", "gpu-1", "cluster-01")]
