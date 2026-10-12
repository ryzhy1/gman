"""Read-only access to the metrics store (Prometheus HTTP API).

Clients pick from fixed query templates; they never send PromQL. Metric and label
names match deploy/otel-agent.example.yaml and deploy/otel-k8s.yaml through the gateway's
Prometheus exporter (verified against otelcol-contrib 0.162.0), the DCGM Exporter's own names and the
recording rules of deploy/alerts/rules.yaml (gman:*).
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,300}$")
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}

# Each template receives `s`, which renders a selector with the request's matchers added.
HOST = {
    "cpu_utilization": lambda s: f'1 - avg by (host_id) (system_cpu_utilization_ratio{s(state="idle")})',
    "cpu_iowait": lambda s: f'avg by (host_id) (system_cpu_utilization_ratio{s(state="wait")})',
    "memory_utilization": lambda s: f'sum by (host_id) (system_memory_utilization_ratio{s(state="used")})',
    "memory_used_bytes": lambda s: f'sum by (host_id) (system_memory_usage_bytes{s(state="used")})',
    "load_1m": lambda s: f'max by (host_id) (system_cpu_load_average_1m{s()})',
    "filesystem_max_utilization": lambda s: f'max by (host_id) (system_filesystem_utilization_ratio{s()})',
    "network_rx_bytes_per_second": lambda s: f'sum by (host_id) (rate(system_network_io_bytes_total{s(direction="receive", device__ne="lo")}[2m]))',
    "network_tx_bytes_per_second": lambda s: f'sum by (host_id) (rate(system_network_io_bytes_total{s(direction="transmit", device__ne="lo")}[2m]))',
    "disk_io_bytes_per_second": lambda s: f'sum by (host_id) (rate(system_disk_io_bytes_total{s()}[2m]))',
}
CONTAINER = {
    # CPU in cores: 1.0 is one fully used core, independent of host size and limits.
    "cpu_cores": lambda s: f'sum by (host_id, container_id) (rate(container_cpu_usage_nanoseconds_total{s()}[2m])) / 1e9',
    "cpu_throttled_seconds_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_cpu_throttling_data_throttled_time_nanoseconds_total{s()}[2m])) / 1e9',
    "memory_bytes": lambda s: f'max by (host_id, container_id) (container_memory_usage_total_bytes{s()})',
    "network_rx_bytes_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_network_io_usage_rx_bytes_total{s()}[2m]))',
    "network_tx_bytes_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_network_io_usage_tx_bytes_total{s()}[2m]))',
    "block_io_bytes_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_blockio_io_service_bytes_recursive_total{s()}[2m]))',
    "pids": lambda s: f'max by (host_id, container_id) (container_pids_count{s()})',
}
# GPU series of deploy/alerts/rules.yaml (group gman-recording): DCGM Exporter or nvidia_gpu_exporter, whichever
# the host runs; memory temperature and the last XID code only come from DCGM.
GPU = {
    "utilization_percent": lambda s: f'max by (host_id, UUID) (gman:gpu_utilization:ratio{s()}) * 100',
    "memory_copy_utilization_percent": lambda s: f'max by (host_id, UUID) (gman:gpu_memory_activity:ratio{s()}) * 100',
    "memory_used_bytes": lambda s: f'max by (host_id, UUID) (gman:gpu_memory_used_bytes:max{s()})',
    "memory_total_bytes": lambda s: f'max by (host_id, UUID) (gman:gpu_memory_total_bytes:max{s()})',
    "temperature_c": lambda s: f'max by (host_id, UUID) (gman:gpu_temperature_celsius:max{s()})',
    "memory_temperature_c": lambda s: f'max by (host_id, UUID) (DCGM_FI_DEV_MEMORY_TEMP{s()})',
    "power_w": lambda s: f'max by (host_id, UUID) (gman:gpu_power_watts:max{s()})',
    "sm_clock_mhz": lambda s: f'max by (host_id, UUID) (gman:gpu_sm_clock_hertz:max{s()}) / 1e6',
    "memory_clock_mhz": lambda s: f'max by (host_id, UUID) (gman:gpu_memory_clock_hertz:max{s()}) / 1e6',
    "last_xid_error": lambda s: f'max by (host_id, UUID) (DCGM_FI_DEV_XID_ERRORS{s()})',
}
# Kubernetes (deploy/otel-k8s.yaml, kubelet_stats receiver). Keyed like the inventory agent's instance id
# "<pod uid>:<container name>". `max by` collapses duplicate series that differ only in labels added by
# k8s_attributes (e.g. right after a collector restart or a pod relabel), so nothing is double counted.
K8S_C = "k8s_pod_uid, k8s_container_name"
K8S_CONTAINER = {
    "cpu_cores": lambda s: f'max by ({K8S_C}) (rate(container_cpu_time_seconds_total{s(k8s_pod_uid__ne="")}[2m]))',
    "memory_bytes": lambda s: f'max by ({K8S_C}) (container_memory_working_set_bytes{s(k8s_pod_uid__ne="")})',
    # Fractions of the container's limit/request (1.0 = at the limit); absent without a limit/request.
    "cpu_limit_utilization": lambda s: f'max by ({K8S_C}) (k8s_container_cpu_limit_utilization_ratio{s()})',
    "cpu_request_utilization": lambda s: f'max by ({K8S_C}) (k8s_container_cpu_request_utilization_ratio{s()})',
    "memory_limit_utilization": lambda s: f'max by ({K8S_C}) (k8s_container_memory_limit_utilization_ratio{s()})',
    "memory_request_utilization": lambda s: f'max by ({K8S_C}) (k8s_container_memory_request_utilization_ratio{s()})',
}
K8S_POD = {
    "network_rx_bytes_per_second": lambda s: 'sum by (k8s_pod_uid) (max by (k8s_pod_uid, interface) '
                                             f'(rate(k8s_pod_network_io_bytes_total{s(direction="receive")}[2m])))',
    "network_tx_bytes_per_second": lambda s: 'sum by (k8s_pod_uid) (max by (k8s_pod_uid, interface) '
                                             f'(rate(k8s_pod_network_io_bytes_total{s(direction="transmit")}[2m])))',
}
# GPUs allocated to a Kubernetes container: DCGM Exporter in Kubernetes mode (GPU Operator default) labels the
# series of a device-plugin GPU with namespace/pod/container. Values are of the whole GPU (shared under
# time-slicing/MPS), so utilization is the max over the container's GPUs and VRAM is the sum over them.
K8S_G = "namespace, pod, container"
K8S_GPU = {
    "gpu_utilization_percent": lambda s: f'max by ({K8S_G}) (DCGM_FI_DEV_GPU_UTIL{s(pod__ne="")})',
    "gpu_memory_used_bytes": lambda s: f'sum by ({K8S_G}) (max by ({K8S_G}, UUID) (DCGM_FI_DEV_FB_USED{s(pod__ne="")})) * 1048576',
}
# vLLM servers per catalog service and model over all replicas (rules.yaml, group gman-llm; 5-minute windows).
# Latency quantiles are absent while a model serves no requests.
LLM_K = "gman_service, model_name"
LLM = {
    "ttft_p50_seconds": lambda s: f'max by ({LLM_K}) (gman:llm_ttft_seconds:p50{s()})',
    "ttft_p95_seconds": lambda s: f'max by ({LLM_K}) (gman:llm_ttft_seconds:p95{s()})',
    "tpot_p95_seconds": lambda s: f'max by ({LLM_K}) (gman:llm_tpot_seconds:p95{s()})',
    "e2e_p95_seconds": lambda s: f'max by ({LLM_K}) (gman:llm_e2e_seconds:p95{s()})',
    "requests_per_second": lambda s: f'max by ({LLM_K}) (gman:llm_requests:rate5m{s()})',
    "generation_tokens_per_second": lambda s: f'max by ({LLM_K}) (gman:llm_generation_tokens:rate5m{s()})',
    "prompt_tokens_per_second": lambda s: f'max by ({LLM_K}) (gman:llm_prompt_tokens:rate5m{s()})',
    "requests_running": lambda s: f'max by ({LLM_K}) (gman:llm_requests_running:sum{s()})',
    "requests_waiting": lambda s: f'max by ({LLM_K}) (gman:llm_requests_waiting:sum{s()})',
    "kv_cache_usage": lambda s: f'max by ({LLM_K}) (gman:llm_kv_cache_usage:max{s()})',
}
# scope: (templates, object key labels, separator of the key in current()).
SCOPES = {"host": (HOST, ("host_id",), "/"), "container": (CONTAINER, ("host_id", "container_id"), "/"),
          "gpu": (GPU, ("host_id", "UUID"), "/"),
          "k8s_container": (K8S_CONTAINER, ("k8s_pod_uid", "k8s_container_name"), ":"),
          "k8s_pod": (K8S_POD, ("k8s_pod_uid",), "/"),
          "k8s_gpu": (K8S_GPU, ("namespace", "pod", "container"), "/"),
          "llm": (LLM, ("gman_service", "model_name"), "/")}

# Tests replace this with a client on a mock transport.
client = httpx.Client(timeout=10)


class Unavailable(Exception):
    pass


def base_url():
    return os.getenv("PROMETHEUS_URL", "").rstrip("/")


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def selector(matchers):
    """Selector factory: static matchers from the template (suffix __ne: !=, __re: =~) plus the request's
    exact matchers."""
    def render(**static):
        ops = {"__ne": "!=", "__re": "=~"}
        parts = [(k[:-4] + ops[k[-4:]] if k[-4:] in ops else k + "=") + quote(v) for k, v in static.items()]
        parts += [k + "=" + quote(v) for k, v in matchers.items()]
        return "{" + ",".join(parts) + "}"
    return render


def request(path, params):
    try:
        response = client.get(base_url() + path, params=params)
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise Unavailable(type(error).__name__) from error
    if response.status_code != 200 or body.get("status") != "success":
        raise Unavailable(f"HTTP {response.status_code}: {str(body.get('error', ''))[:200]}")
    return body["data"]["result"]


def number(value):
    result = float(value)
    return None if result != result or result in (float("inf"), float("-inf")) else result


def current():
    """Latest value of every template, keyed by scope and then by object: "host_id/object_id" for
    Docker hosts, "pod_uid:container_name" (the inventory instance id) and "pod_uid" for Kubernetes,
    "namespace/pod/container" for GPUs allocated to Kubernetes containers, "service/model" for LLM servers."""
    jobs = [(scope, name, template(selector({})), keys, sep)
            for scope, (templates, keys, sep) in SCOPES.items() for name, template in templates.items()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: request("/api/v1/query", {"query": job[2]}), jobs))
    data = {scope: {} for scope in SCOPES}
    for (scope, name, _, keys, sep), series in zip(jobs, results):
        for item in series:
            key = sep.join(item["metric"].get(k, "") for k in keys)
            data[scope].setdefault(key, {})[name] = number(item["value"][1])
    return data


def series(scope, name, matchers, range_name):
    templates, keys, _ = SCOPES[scope]
    if name not in templates or range_name not in RANGES or set(matchers) != set(keys):
        raise KeyError("Unknown metric, range or object")
    for value in matchers.values():
        if not LABEL.match(value):
            raise KeyError("Invalid identifier")
    seconds = RANGES[range_name]
    end = datetime.now(timezone.utc).timestamp()
    step = max(15, seconds // 240)
    result = request("/api/v1/query_range", {"query": templates[name](selector(matchers)),
                                             "start": end - seconds, "end": end, "step": step})
    points = result[0]["values"] if result else []
    return {"scope": scope, "metric": name, "range": range_name, "step": step,
            "points": [[float(t), number(v)] for t, v in points]}
