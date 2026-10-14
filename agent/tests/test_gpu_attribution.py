"""GPU process attribution on Kubernetes nodes: /proc/<pid>/cgroup -> container ID -> pod."""
import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import collect
from collection.adapters.proc_cgroups import ProcCgroups
from collection.entity import ProcessOwner
from collection.orchestrator import CollectionOrchestrator
from collection.services.kubernetes_inventory import KubernetesInventory
from config.settings import load_settings
from conftest import FakeRunner, collection, runtime
from runtime_access.kubernetes_api import InClusterAccess, KubernetesApi

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
    assert ProcCgroups.parse(text) == ProcessOwner(container_id=container, pod_uid=pod)


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
def node(tmp_path):
    """Fixture /proc with cgroup files; mocked nvidia-smi (base fields only) and kubectl."""
    for pid, text in CGROUP_BY_PID.items():
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cgroup").write_text(text)
    # PID 600 exited: no /proc entry.
    state = SimpleNamespace(calls=[], kubectl_missing=False)

    def fake(args):
        state.calls.append(args)
        if args[0] == "kubectl":
            if state.kubectl_missing:
                raise FileNotFoundError("kubectl")
            return json.dumps(PODS)
        if "clocks_throttle_reasons.active" in args[1]:
            raise subprocess.CalledProcessError(2, args)
        return QUERY + "\n" if args[1].startswith("--query-gpu") else APPS

    state.agent = collection("kubernetes-node", "cluster-01.gpu-1", "gpu-1", "cluster-01", handler=fake,
                             proc_root=str(tmp_path))
    return state


def test_node_snapshot_attributes_processes_to_pods(node, api_schemas):
    data = node.agent.snapshot()
    assert ["kubectl", "get", "pods", "--all-namespaces", "--field-selector", "spec.nodeName=gpu-1", "-o", "json"] in node.calls
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
    api_schemas.Snapshot(**data)


def test_node_snapshot_without_pod_list_keeps_cgroup_ids(node):
    node.kubectl_missing = True
    data = node.agent.snapshot()
    assert "pods of the node unavailable: FileNotFoundError" in data["errors"]
    process = data["gpus"][0]["processes"][0]
    assert (process["container_id"], process["pod_uid"], "pod" in process) == (CID, UID, False)


def test_kube_get_falls_back_to_in_cluster_api(tmp_path):
    (tmp_path / "token").write_text("sa-token\n")
    (tmp_path / "ca.crt").write_text("")
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
    api = KubernetesApi(FakeRunner(), InClusterAccess("10.0.0.1", "6443", tmp_path), opener=fake_urlopen,
                        context_factory=lambda cafile: ("context", cafile))
    assert len(KubernetesInventory(api).node_pods("gpu-1")) == 3
    assert seen["url"] == "https://10.0.0.1:6443/api/v1/pods?fieldSelector=spec.nodeName%3Dgpu-1"
    assert seen["auth"] == "Bearer sa-token" and seen["context"] == ("context", str(tmp_path / "ca.crt"))


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

    def fake(self):
        target = self._target
        seen.append((target.runtime, target.source_id, target.node, target.cluster))
        raise RuntimeError("stop before sending")

    monkeypatch.setattr(CollectionOrchestrator, "snapshot", fake)
    with pytest.raises(RuntimeError, match="stop"):
        collect.main(argv, load_settings(env), runtime())
    assert seen == [("kubernetes-node", "cluster-01.gpu-1", "gpu-1", "cluster-01")]
