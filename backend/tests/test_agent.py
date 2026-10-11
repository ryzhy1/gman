import json
import subprocess

from agent import bamboo_event, collect
from agent.collect import docker_instance, pod_instances, quantity


def test_docker_allowlist_and_cpu_quota():
    result = docker_instance({"Id":"id", "Config":{"Image":"img", "Env":["SECRET=abc", "NVIDIA_VISIBLE_DEVICES=0,1"],
                              "Cmd":["--password=abc"], "Healthcheck":{"Test":["CMD", "curl", "-u", "abc"]},
                              "Labels":{"secret":"abc", "gman.service":"service",
                                        "org.opencontainers.image.revision":"deadbeef",
                                        "com.docker.compose.project":"shop", "com.docker.compose.service":"api"}},
                              "HostConfig":{"CpuQuota":200000,"CpuPeriod":100000,"Memory":0},
                              "State":{"Status":"running"}})
    assert "abc" not in str(result)
    assert result["config"]["cpu_limit"] == 2
    assert result["config"]["memory_limit_bytes"] is None
    assert result["config"]["gpu_requests"] == ["NVIDIA_VISIBLE_DEVICES=0,1"]
    assert result["config"]["probes"] == ["healthcheck interval= timeout= retries=0"]
    assert result["service_id"] == "service"
    assert result["controller"] == "shop/api"
    assert result["origin"] == {"image_revision": "deadbeef", "compose_project": "shop", "compose_service": "api"}


def test_docker_state_is_not_masked_by_stale_health():
    result = docker_instance({"Id":"x", "Created":"2026-10-10T10:00:00Z",
                              "State":{"Status":"exited", "ExitCode":137, "OOMKilled":True,
                                       "Health":{"Status":"healthy"}, "StartedAt":"0001-01-01T00:00:00Z"}})
    assert (result["status"], result["health"], result["reason"], result["exit_code"]) == ("exited", "", "OOMKilled", 137)
    assert result["started_at"] is None
    assert result["created_at"] == "2026-10-10T10:00:00Z"


def test_container_removed_during_collection(monkeypatch):
    def fake(args):
        if args[:2] == ["docker", "ps"]:
            return "a\nb\n" if not calls else "a\n"
        calls.append(args)
        if "b" in args:
            raise subprocess.CalledProcessError(1, args)
        return json.dumps([{"Id": "a"}])
    calls = []
    monkeypatch.setattr(collect, "command", fake)
    assert [c["Id"] for c in collect.inspect_containers()] == ["a"]


def test_daemon_failure_is_not_a_removal(monkeypatch):
    def fake(args):
        if args[:2] == ["docker", "ps"]:
            return "a\n"
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(collect, "command", fake)
    try:
        collect.inspect_containers()
        assert False, "partial snapshot must not be produced"
    except RuntimeError:
        pass


def test_nvidia_failure_is_reported(monkeypatch):
    def fake(args):
        raise subprocess.CalledProcessError(9, args)
    monkeypatch.setattr(collect, "command", fake)
    errors = []
    assert not [g for g in collect.gpus(errors) if g["vendor"] == "NVIDIA"]
    assert errors == ["nvidia-smi failed with exit code 9"]


def test_pod_identity_limits_and_no_env():
    pod = {"metadata":{"uid":"uid", "name":"pod", "labels":{"gman.service":"svc"}, "creationTimestamp":"2026-10-10T10:00:00Z",
                       "ownerReferences":[{"kind":"ReplicaSet", "name":"pod-7d9", "uid":"rs", "controller":True}],
                       "managedFields":[{"manager":"kube-controller-manager"}]},
           "spec":{"nodeName":"node", "containers":[{"name":"app", "image":"img",
                    "env":[{"name":"SECRET","value":"abc"}],
                    "livenessProbe":{"exec":{"command":["sh", "-c", "abc"]}},
                    "readinessProbe":{"httpGet":{"path":"/abc", "port":8080}, "periodSeconds":5},
                    "resources":{"limits":{"cpu":"500m","memory":"2Gi","nvidia.com/gpu":"1"},
                                 "requests":{"cpu":"250m","memory":"1Gi"}}}]},
           "status":{"containerStatuses":[{"name":"app", "ready":False, "restartCount":4,
                                           "state":{"waiting":{"reason":"CrashLoopBackOff"}},
                                           "lastState":{"terminated":{"reason":"OOMKilled", "exitCode":137}}}]}}
    result = pod_instances(pod)[0]
    assert result["id"] == "uid:app"
    assert result["controller"] == "ReplicaSet/pod-7d9"
    assert (result["status"], result["reason"], result["ready"]) == ("waiting", "CrashLoopBackOff", False)
    assert result["last_termination_reason"] == "OOMKilled exit 137"
    assert result["origin"]["field_managers"] == "kube-controller-manager"
    assert result["config"]["memory_limit_bytes"] == 2 * 1024**3
    assert result["config"]["memory_request_bytes"] == 1024**3
    assert result["config"]["cpu_limit"] == .5
    assert result["config"]["cpu_request"] == .25
    assert result["config"]["gpu_requests"] == ["nvidia.com/gpu=1"]
    assert result["config"]["probes"] == ["liveness exec period=10s failure=3", "readiness http :8080 period=5s failure=3"]
    assert "abc" not in str(result)
    assert quantity("unsupported") is None


def test_bamboo_defaults_are_stable_across_retries(monkeypatch):
    for key, value in {"GMAN_SERVICE_ID": "api", "GMAN_DEPLOYMENT_STATUS": "succeeded",
                       "bamboo_deploy_environment": "prod", "bamboo_buildResultKey": "PRJ-PLAN-12",
                       "bamboo_deploy_release": "release 7", "bamboo_ManualBuildTriggerReason_userName": "ivanov"}.items():
        monkeypatch.setenv(key, value)
    first = bamboo_event.payload()
    assert first == bamboo_event.payload()
    assert "occurred_at" not in first
    assert first["event_id"] == "bamboo:PRJ-PLAN-12:release-7:prod:succeeded"
    assert (first["actor"], first["actor_kind"]) == ("ivanov", "human")


def test_nvidia_details_and_process_attribution(monkeypatch, tmp_path):
    base = "GPU-1, NVIDIA A100-SXM4-80GB, 00000000:3B:00.0, 550.54.15, P0, 37, 12, 81920, 40960, 61, 251.50"
    extended = ", 70, 400.00, 1410, 1593, 1410, [N/A], 0x0000000000000044, 0, 4, 16, Disabled, Enabled"
    def fake(args):
        if args[1].startswith("--query-gpu"):
            return base + extended + "\n"
        return "GPU-1, 4242, 1024\nGPU-1, 77, [N/A]\n"
    monkeypatch.setattr(collect, "command", fake)
    monkeypatch.setattr(collect, "cgroup_of", lambda pid: {"container_id": "c" * 64 if pid == "4242" else "", "pod_uid": ""})
    errors = []
    gpu = collect.nvidia_gpus(errors)[0]
    assert errors == []
    assert (gpu["model"], gpu["driver_version"], gpu["pstate"]) == ("NVIDIA A100-SXM4-80GB", "550.54.15", "P0")
    assert gpu["memory_used_bytes"] == 40960 * 1024**2 and gpu["memory_temperature_c"] == 70
    assert gpu["fan_percent"] is None  # [N/A] is absence, not zero.
    assert gpu["throttle_reasons"] == ["sw_power_cap", "hw_thermal"]
    assert (gpu["pcie_generation"], gpu["pcie_width"], gpu["ecc_uncorrected_errors"]) == (4, 16, 0)
    assert gpu["processes"] == [{"pid": 4242, "container_id": "c" * 64, "used_memory_bytes": 1024**3},
                                {"pid": 77, "container_id": "", "used_memory_bytes": None}]
    from backend.app.main import GPU
    GPU(**gpu)


def test_nvidia_old_driver_falls_back_to_base_fields(monkeypatch):
    def fake(args):
        if "clocks_throttle_reasons.active" in args[1]:
            raise subprocess.CalledProcessError(2, args)
        if args[1].startswith("--query-gpu"):
            return "GPU-1, Tesla T4, 00000000:3B:00.0, 470.1, P8, 0, 0, 15360, 0, 30, 9.5\n"
        return ""
    monkeypatch.setattr(collect, "command", fake)
    errors = []
    gpu = collect.nvidia_gpus(errors)[0]
    assert gpu["model"] == "Tesla T4" and gpu["power_w"] == 9.5 and "sm_clock_mhz" not in gpu
    assert errors == ["nvidia-smi: extended GPU fields unsupported by this driver"]


def test_container_of_reads_cgroup(monkeypatch, tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/docker-" + "ab" * 32 + ".scope\n")
    (tmp_path / "1").mkdir()
    cgroup.rename(tmp_path / "1" / "cgroup")
    monkeypatch.setattr(collect, "PROC_ROOT", str(tmp_path))
    assert collect.container_of("1") == "ab" * 32
    assert collect.container_of("2") == ""  # Exited process.


def test_node_capacity_and_requests():
    node = {"metadata": {"name": "gpu-1", "labels": {"node-role.kubernetes.io/worker": "", "nvidia.com/gpu.product": "NVIDIA-A100"}},
            "spec": {"taints": [{"key": "gpu", "effect": "NoSchedule"}]},
            "status": {"capacity": {"cpu": "64", "memory": "512Gi", "nvidia.com/gpu": "8"},
                       "allocatable": {"cpu": "63500m", "memory": "500Gi", "nvidia.com/gpu": "8"},
                       "conditions": [{"type": "Ready", "status": "True"}, {"type": "DiskPressure", "status": "True"}],
                       "nodeInfo": {"kubeletVersion": "v1.31.2"}}}
    def pod(phase, cpu, gpu):
        return {"spec": {"nodeName": "gpu-1", "containers": [{"resources": {"requests": {"cpu": cpu, "memory": "1Gi"},
                                                                            "limits": {"nvidia.com/gpu": gpu}}}]},
                "status": {"phase": phase}}
    result = collect.nodes([node], [pod("Running", "500m", "2"), pod("Succeeded", "4", "4")])[0]
    assert (result["ready"], result["pressure"], result["roles"], result["taints"]) == (True, ["DiskPressure"], ["worker"], ["gpu:NoSchedule"])
    assert (result["cpu_capacity"], result["cpu_allocatable"], result["cpu_requested"]) == (64, 63.5, 0.5)
    assert (result["gpu_capacity"], result["gpu_requested"], result["memory_requested_bytes"]) == (8, 2, 1024**3)
    from backend.app.main import Node
    Node(**result)
