import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from collection.adapters.amd_sysfs import AmdSysfs
from collection.adapters.host_system import HostSystem
from collection.adapters.nvidia_smi import NvidiaSmi
from collection.adapters.proc_cgroups import ProcCgroups
from collection.entity import DaemonInspectError, ProcessOwner
from collection.mappers.docker_mapper import docker_instance
from collection.mappers.kubernetes_node_mapper import nodes
from collection.mappers.kubernetes_pod_mapper import pod_instances
from collection.mappers.kubernetes_quantity import quantity
from collection.services.docker_inventory import DockerInventory
from collection.services.env_file_reader import ENV_FILES, EnvFileReader, parse_dotenv
from collection.services.gpu_inventory import GpuInventory
from conftest import FakeRunner, settings
from deployment_report.container import build_deployment_report
from runtime_access.docker_cli import DockerCli

NO_SYSFS = Path("/nonexistent")


def gpu_inventory(fake, cgroups=None):
    runner = FakeRunner(fake)
    return GpuInventory(NvidiaSmi(runner), AmdSysfs(NO_SYSFS), cgroups or ProcCgroups(NO_SYSFS))


def bamboo_event(env):
    return build_deployment_report(settings(gman_url="https://gman.example")).event(env)


def test_docker_allowlist_and_cpu_quota():
    result = docker_instance({"Id":"id", "Config":{"Image":"img", "Env":["SECRET=abc", "NVIDIA_VISIBLE_DEVICES=0,1"],
                              "Entrypoint":["vllm", "serve"], "Cmd":["--password=abc"], "Healthcheck":{"Test":["CMD", "curl", "-u", "abc"]},
                              "Labels":{"secret":"abc", "gman.service":"service",
                                        "org.opencontainers.image.revision":"deadbeef",
                                        "com.docker.compose.project":"shop", "com.docker.compose.service":"api"}},
                              "HostConfig":{"CpuQuota":200000,"CpuPeriod":100000,"Memory":0},
                              "State":{"Status":"running"}})
    # Environment and launch parameters are reported in full; healthcheck commands and other labels still are not.
    assert result["config"]["env"] == ["SECRET=abc", "NVIDIA_VISIBLE_DEVICES=0,1"]
    assert (result["config"]["command"], result["config"]["args"]) == (["vllm", "serve"], ["--password=abc"])
    assert "abc" not in str({**result, "config": {**result["config"], "env": [], "args": []}})
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


def test_container_removed_during_collection():
    def fake(args):
        if args[:2] == ["docker", "ps"]:
            return "a\nb\n" if not calls else "a\n"
        calls.append(args)
        if "b" in args:
            raise subprocess.CalledProcessError(1, args)
        return json.dumps([{"Id": "a"}])
    calls = []
    assert [c["Id"] for c in DockerInventory(DockerCli(FakeRunner(fake))).inspect_containers()] == ["a"]


def test_daemon_failure_is_not_a_removal():
    def fake(args):
        if args[:2] == ["docker", "ps"]:
            return "a\n"
        raise subprocess.CalledProcessError(1, args)
    # A partial snapshot must not be produced.
    with pytest.raises(DaemonInspectError):
        DockerInventory(DockerCli(FakeRunner(fake))).inspect_containers()


def test_nvidia_failure_is_reported():
    def fake(args):
        raise subprocess.CalledProcessError(9, args)
    errors = []
    assert not [g for g in gpu_inventory(fake).gpus(errors) if g["vendor"] == "NVIDIA"]
    assert errors == ["nvidia-smi failed with exit code 9"]


def test_pod_identity_limits_and_env():
    pod = {"metadata":{"uid":"uid", "name":"pod", "labels":{"gman.service":"svc"}, "creationTimestamp":"2026-10-10T10:00:00Z",
                       "ownerReferences":[{"kind":"ReplicaSet", "name":"pod-7d9", "uid":"rs", "controller":True}],
                       "managedFields":[{"manager":"kube-controller-manager"}]},
           "spec":{"nodeName":"node", "containers":[{"name":"app", "image":"img",
                    "args":["--model", "llama", "--api-key=abc"],
                    "env":[{"name":"SECRET","value":"abc"},
                           {"name":"DB_PASSWORD","valueFrom":{"secretKeyRef":{"name":"db","key":"password"}}},
                           {"name":"NODE","valueFrom":{"fieldRef":{"fieldPath":"spec.nodeName"}}}],
                    "envFrom":[{"configMapRef":{"name":"app-config"}}, {"secretRef":{"name":"app-secrets"}, "prefix":"APP_"}],
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
    assert result["config"]["env"] == ["SECRET=abc", "DB_PASSWORD=<secretKeyRef db/password>",
                                       "NODE=<fieldRef spec.nodeName>", "<envFrom>=<configMapRef app-config>",
                                       "<envFrom>=<secretRef app-secrets prefix=APP_>"]
    # Only the Pod's overrides: no command means the image's ENTRYPOINT.
    assert (result["config"]["command"], result["config"]["args"]) == ([], ["--model", "llama", "--api-key=abc"])
    assert "abc" not in str({**result, "config": {**result["config"], "env": [], "args": []}})
    assert quantity("unsupported") is None


def test_bamboo_defaults_are_stable_across_retries():
    env = {"GMAN_SERVICE_ID": "api", "GMAN_DEPLOYMENT_STATUS": "succeeded",
           "bamboo_deploy_environment": "prod", "bamboo_buildResultKey": "PRJ-PLAN-12",
           "bamboo_deploy_release": "release 7", "bamboo_ManualBuildTriggerReason_userName": "ivanov"}
    first = bamboo_event(env)
    assert first == bamboo_event(env)
    assert "occurred_at" not in first
    assert first["event_id"] == "bamboo:PRJ-PLAN-12:release-7:prod:succeeded"
    assert (first["actor"], first["actor_kind"]) == ("ivanov", "human")


def test_bamboo_deployment_run_and_trigger():
    env = {"GMAN_SERVICE_ID": "api", "GMAN_DEPLOYMENT_STATUS": "started",
           "bamboo_deploy_environment": "prod", "bamboo_buildResultKey": "PRJ-PLAN-12",
           "bamboo_deploy_version": "release-8", "bamboo_planRepository_1_revision": "abc123",
           "bamboo_resultsUrl": "https://bamboo.example/deploy/viewDeploymentResult.action?deploymentResultId=4242",
           "bamboo_triggerReason_key": "com.atlassian.bamboo.plugin.system.triggerReason:DependencyTriggerReason",
           "bamboo_ManualBuildTriggerReason_userName": "ivanov",
           "GMAN_IMAGE_DIGEST": "sha256:" + "b" * 64}
    value = bamboo_event(env)
    # A redeploy of the same release is a different deployment result, hence a different event.
    assert (value["event_id"], value["deployment_id"]) == ("bamboo:bamboo-deploy-4242:started", "bamboo-deploy-4242")
    assert (value["release"], value["commit"]) == ("release-8", "abc123")
    # Automatic trigger: the user name belongs to the build, not to this deployment.
    assert (value["actor"], value["actor_kind"]) == ("unknown", "unknown")
    env["bamboo_triggerReason_key"] = "com.atlassian.bamboo.plugin.system.triggerReason:ManualBuildTriggerReason"
    assert (bamboo_event(env)["actor"], bamboo_event(env)["actor_kind"]) == ("ivanov", "human")
    env["bamboo_triggerReason_key"] = "com.atlassian.bamboo.plugin.system.triggerReason:ScheduledTriggerReason"
    assert bamboo_event(env)["actor_kind"] == "schedule"


class FakeCgroups:
    def owner(self, pid):
        return ProcessOwner(container_id="c" * 64 if pid == 4242 else "", pod_uid="")


def test_nvidia_details_and_process_attribution(api_schemas):
    base = "GPU-1, NVIDIA A100-SXM4-80GB, 00000000:3B:00.0, 550.54.15, P0, 37, 12, 81920, 40960, 61, 251.50"
    extended = ", 70, 400.00, 1410, 1593, 1410, [N/A], 0x0000000000000044, 0, 4, 16, Disabled, Enabled"
    def fake(args):
        if args[1].startswith("--query-gpu"):
            return base + extended + "\n"
        return "GPU-1, 4242, 1024\nGPU-1, 77, [N/A]\n"
    errors = []
    card = gpu_inventory(fake, FakeCgroups()).nvidia_gpus(errors)[0]
    assert errors == []
    assert (card["model"], card["driver_version"], card["pstate"]) == ("NVIDIA A100-SXM4-80GB", "550.54.15", "P0")
    assert card["memory_used_bytes"] == 40960 * 1024**2 and card["memory_temperature_c"] == 70
    assert card["fan_percent"] is None  # [N/A] is absence, not zero.
    assert card["throttle_reasons"] == ["sw_power_cap", "hw_thermal"]
    assert (card["pcie_generation"], card["pcie_width"], card["ecc_uncorrected_errors"]) == (4, 16, 0)
    assert card["processes"] == [{"pid": 4242, "container_id": "c" * 64, "used_memory_bytes": 1024**3},
                                 {"pid": 77, "container_id": "", "used_memory_bytes": None}]
    api_schemas.GPU(**card)


def test_nvidia_old_driver_falls_back_to_base_fields():
    def fake(args):
        if "clocks_throttle_reasons.active" in args[1]:
            raise subprocess.CalledProcessError(2, args)
        if args[1].startswith("--query-gpu"):
            return "GPU-1, Tesla T4, 00000000:3B:00.0, 470.1, P8, 0, 0, 15360, 0, 30, 9.5\n"
        return ""
    errors = []
    card = gpu_inventory(fake).nvidia_gpus(errors)[0]
    assert card["model"] == "Tesla T4" and card["power_w"] == 9.5 and "sm_clock_mhz" not in card
    assert errors == ["nvidia-smi: extended GPU fields unsupported by this driver"]


def test_container_of_reads_cgroup(tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/docker-" + "ab" * 32 + ".scope\n")
    (tmp_path / "1").mkdir()
    cgroup.rename(tmp_path / "1" / "cgroup")
    cgroups = ProcCgroups(tmp_path)
    assert cgroups.container_of(1) == "ab" * 32
    assert cgroups.container_of(2) == ""  # Exited process.


def test_node_capacity_and_requests(api_schemas):
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
    result = nodes([node], [pod("Running", "500m", "2"), pod("Succeeded", "4", "4")])[0]
    assert (result["ready"], result["pressure"], result["roles"], result["taints"]) == (True, ["DiskPressure"], ["worker"], ["gpu:NoSchedule"])
    assert (result["cpu_capacity"], result["cpu_allocatable"], result["cpu_requested"]) == (64, 63.5, 0.5)
    assert (result["gpu_capacity"], result["gpu_requested"], result["memory_requested_bytes"]) == (8, 2, 1024**3)
    api_schemas.Node(**result)


def test_host_ip_override():
    runner = FakeRunner()
    assert HostSystem(runner, DockerCli(runner), "203.0.113.7").host_ip() == "203.0.113.7"


def test_node_internal_ip():
    node = {'metadata': {'name': 'n1'}, 'status': {'addresses': [
        {'type': 'Hostname', 'address': 'n1'}, {'type': 'InternalIP', 'address': '10.0.0.5'}]}}
    assert nodes([node], [])[0]['internal_ip'] == '10.0.0.5'


def test_parse_dotenv():
    text = "# comment\n\nENV=local\nexport TIMEOUT=5s\nREDIS_PASSWORD=\nQUOTED=\"a b # c\"\nSINGLE='x'\n" \
           "POSTGRES_CONN=postgres://u:p@db:5432/db?sslmode=disable\nNOTE=value # trailing\nnot a variable\n"
    assert parse_dotenv(text) == [
        "ENV=local", "TIMEOUT=5s", "REDIS_PASSWORD=", "QUOTED=a b # c", "SINGLE=x",
        "POSTGRES_CONN=postgres://u:p@db:5432/db?sslmode=disable", "NOTE=value"]


def tar_of(name, data):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_read_container_file_uses_docker_cp():
    calls = []

    def run(args, capture_output, timeout):
        calls.append(args)
        found = args[2] == "abc:/app/.env.local"
        return subprocess.CompletedProcess(args, 0 if found else 1, tar_of(".env.local", b"A=1\n") if found else b"")

    docker = DockerCli(FakeRunner(run=run))
    assert docker.read_file("abc", "/app/.env.local", 1024) == "A=1\n"
    assert docker.read_file("abc", "/app/.env", 1024) is None
    assert calls[0] == ["docker", "cp", "abc:/app/.env.local", "-"]


class FakeDocker:
    def __init__(self, read):
        self.read = read

    def read_file(self, container_id, path, max_bytes):
        return self.read(container_id, path)


def test_env_files_from_working_dir_read_once_per_start():
    reads = []

    def read(cid, path):
        reads.append(path)
        return "JWT_SECRET=s\nKAFKA=kafka:9092\n" if path == "/app/.env.local" else None

    reader = EnvFileReader(FakeDocker(read))
    assert reader.env_files("abc", "t1", "/app/") == {"/app/.env.local": ["JWT_SECRET=s", "KAFKA=kafka:9092"]}
    assert len(reads) == len(ENV_FILES) and reads[0] == "/app/.env"
    reader.env_files("abc", "t1", "/app/")
    assert len(reads) == len(ENV_FILES)  # cached while the container keeps running
    reader.env_files("abc", "t2", "/app/")
    assert len(reads) == 2 * len(ENV_FILES)  # restarted: read again
    assert EnvFileReader(FakeDocker(lambda cid, path: None)).env_files("x", "", "/") == {}
