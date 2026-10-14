"""Фиксированные шаблоны PromQL: клиенты выбирают шаблон по имени и никогда не присылают PromQL.

Имена метрик и меток совпадают с deploy/otel-agent.example.yaml и deploy/otel-k8s.yaml (через Prometheus
exporter шлюза, проверено на otelcol-contrib 0.162.0), собственными именами DCGM Exporter и правилами
записи deploy/alerts/rules.yaml (gman:*).
"""
import re
from collections.abc import Callable
from dataclasses import dataclass

# Значения меток из запроса: идентификаторы, без кавычек и выражений.
LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,300}$")
RANGE_SECONDS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
# Суффиксы статических матчеров шаблона: label__ne="x" -> label!="x", label__re="x" -> label=~"x".
MATCHER_OPERATORS = {"__ne": "!=", "__re": "=~"}
MATCHER_SUFFIX_LENGTH = 4
EQUALS = "="

Template = Callable[["LabelSelector"], str]


class LabelSelector:
    """Строит селектор `{...}`: статические матчеры шаблона плюс точные матчеры запроса."""

    def __init__(self, matchers: dict[str, str]) -> None:
        """Запоминает точные матчеры запроса (метка -> значение)."""
        self._matchers = matchers

    def __call__(self, **static: str) -> str:
        """Селектор со статическими матчерами шаблона и матчерами запроса."""
        parts = []
        for key, value in static.items():
            suffix = key[-MATCHER_SUFFIX_LENGTH:]
            if suffix in MATCHER_OPERATORS:
                parts.append(key[:-MATCHER_SUFFIX_LENGTH] + MATCHER_OPERATORS[suffix] + quote_label(value))
            else:
                parts.append(key + EQUALS + quote_label(value))
        for key, value in self._matchers.items():
            parts.append(key + EQUALS + quote_label(value))
        selector = "{" + ",".join(parts) + "}"
        return selector


def quote_label(value: str) -> str:
    """Значение метки в кавычках PromQL с экранированием."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    quoted = '"' + escaped + '"'
    return quoted


@dataclass(frozen=True)
class ScopeTemplates:
    """Шаблоны одной области и метки, из которых складывается ключ объекта."""

    templates: dict[str, Template]
    key_labels: tuple[str, ...]
    key_separator: str


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
    # CPU в ядрах: 1.0 — одно полностью занятое ядро, независимо от размера хоста и лимитов.
    "cpu_cores": lambda s: f'sum by (host_id, container_id) (rate(container_cpu_usage_nanoseconds_total{s()}[2m])) / 1e9',
    "cpu_throttled_seconds_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_cpu_throttling_data_throttled_time_nanoseconds_total{s()}[2m])) / 1e9',
    "memory_bytes": lambda s: f'max by (host_id, container_id) (container_memory_usage_total_bytes{s()})',
    "network_rx_bytes_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_network_io_usage_rx_bytes_total{s()}[2m]))',
    "network_tx_bytes_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_network_io_usage_tx_bytes_total{s()}[2m]))',
    "block_io_bytes_per_second": lambda s: f'sum by (host_id, container_id) (rate(container_blockio_io_service_bytes_recursive_total{s()}[2m]))',
    "pids": lambda s: f'max by (host_id, container_id) (container_pids_count{s()})',
}
# Ряды GPU из deploy/alerts/rules.yaml (группа gman-recording): DCGM Exporter или nvidia_gpu_exporter, что есть
# на хосте; температура памяти и последний код XID приходят только от DCGM.
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
# Kubernetes (deploy/otel-k8s.yaml, приемник kubelet_stats). Ключ как ID экземпляра агента
# "<pod uid>:<container name>". `max by` схлопывает дубли рядов, отличающиеся только метками k8s_attributes
# (например, сразу после рестарта коллектора), поэтому ничего не считается дважды.
K8S_C = "k8s_pod_uid, k8s_container_name"
K8S_CONTAINER = {
    "cpu_cores": lambda s: f'max by ({K8S_C}) (rate(container_cpu_time_seconds_total{s(k8s_pod_uid__ne="")}[2m]))',
    "memory_bytes": lambda s: f'max by ({K8S_C}) (container_memory_working_set_bytes{s(k8s_pod_uid__ne="")})',
    # Доли лимита/запроса контейнера (1.0 — на лимите); отсутствуют без лимита/запроса.
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
# GPU контейнера Kubernetes: DCGM Exporter в режиме Kubernetes (по умолчанию в GPU Operator) метит ряды GPU
# device plugin-а namespace/pod/container. Значения — по всему GPU (общему при time-slicing/MPS), поэтому
# утилизация — максимум по GPU контейнера, а VRAM — сумма.
K8S_G = "namespace, pod, container"
K8S_GPU = {
    "gpu_utilization_percent": lambda s: f'max by ({K8S_G}) (DCGM_FI_DEV_GPU_UTIL{s(pod__ne="")})',
    "gpu_memory_used_bytes": lambda s: f'sum by ({K8S_G}) (max by ({K8S_G}, UUID) (DCGM_FI_DEV_FB_USED{s(pod__ne="")})) * 1048576',
}
# vLLM-серверы по сервису каталога и модели по всем репликам (rules.yaml, группа gman-llm; окна 5 минут).
# Квантили задержек отсутствуют, пока модель не обслуживает запросы.
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
SLASH = "/"
COLON = ":"
SCOPES = {
    "host": ScopeTemplates(HOST, ("host_id",), SLASH),
    "container": ScopeTemplates(CONTAINER, ("host_id", "container_id"), SLASH),
    "gpu": ScopeTemplates(GPU, ("host_id", "UUID"), SLASH),
    "k8s_container": ScopeTemplates(K8S_CONTAINER, ("k8s_pod_uid", "k8s_container_name"), COLON),
    "k8s_pod": ScopeTemplates(K8S_POD, ("k8s_pod_uid",), SLASH),
    "k8s_gpu": ScopeTemplates(K8S_GPU, ("namespace", "pod", "container"), SLASH),
    "llm": ScopeTemplates(LLM, ("gman_service", "model_name"), SLASH),
}
