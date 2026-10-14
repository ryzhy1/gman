"""Текстовый формат Prometheus: описания метрик, экранирование меток и вывод выборок."""
from exposition.entity import MetricSample

GAUGE = "gauge"
COUNTER = "counter"

# Порядок задает порядок метрик в выводе.
METRIC_HELP = {
    "gman_source_last_snapshot_timestamp_seconds": (GAUGE, "Server receive time of the last accepted snapshot."),
    "gman_source_collect_errors": (GAUGE, "Partial collection errors reported in the last snapshot."),
    "gman_k8s_node_ready": (GAUGE, "Kubernetes node Ready condition from the last snapshot (1 ready, 0 not)."),
    "gman_k8s_node_pressure": (GAUGE, "1 for each node condition other than Ready that is True in the last "
                                      "snapshot (MemoryPressure, DiskPressure, PIDPressure, NetworkUnavailable)."),
    "gman_k8s_node_event_last_timestamp_seconds": (GAUGE, "Last time of an allowlisted Kubernetes Event of the "
                                                          "node with this reason (OOMKilling, SystemOOM, "
                                                          "pressure, Rebooted, KernelOops...)."),
    "gman_instance_restarts_total": (COUNTER, "Restart count of the container as reported by the runtime."),
    "gman_instance_state_reason": (GAUGE, "1 for the current state reason of the container (allowlisted)."),
    "gman_instance_last_termination_reason": (GAUGE, "1 for the reason of the previous termination (Kubernetes)."),
    "gman_instance_last_oom_timestamp_seconds": (GAUGE, "Time of the latest OOM kill of the container (Docker oom "
                                                        "event or OOMKilled, Kubernetes termination)."),
    "gman_gpu_temperature_celsius": (GAUGE, "GPU core temperature from nvidia-smi."),
    "gman_gpu_ecc_uncorrected_errors": (GAUGE, "Volatile uncorrected ECC errors from nvidia-smi."),
    "gman_gpu_clock_event_reasons": (GAUGE, "Active clock throttle reasons from nvidia-smi as a DCGM bitmask."),
    "gman_release_status": (GAUGE, "1 for the status of the latest release of a service/environment (releases)."),
    "gman_release_started_timestamp_seconds": (GAUGE, "Start of the latest release of a service/environment."),
    "gman_tls_cert_not_after_timestamp_seconds": (GAUGE, "notAfter of each certificate in a watched PEM file "
                                                         "(GMAN_TLS_WATCH_FILES)."),
    "gman_tls_crl_next_update_timestamp_seconds": (GAUGE, "nextUpdate of a watched CRL: past it nginx rejects "
                                                          "every OTLP client certificate."),
    "gman_tls_file_error": (GAUGE, "1 when a watched TLS file is missing, unreadable, unparseable or refused "
                                   "(a key file)."),
}


class TextFormat:
    """Выводит выборки в текстовом формате экспозиции Prometheus."""

    def render(self, samples: list[MetricSample]) -> str:
        """Все метрики по порядку описаний: HELP, TYPE и выборки каждой."""
        lines_by_name: dict[str, list[str]] = {name: [] for name in METRIC_HELP}
        for sample in samples:
            lines_by_name[sample.name].append(self.line(sample))
        output = []
        for name, (kind, description) in METRIC_HELP.items():
            output += [f"# HELP {name} {description}", f"# TYPE {name} {kind}", *lines_by_name[name]]
        text = "\n".join(output) + "\n"
        return text

    def line(self, sample: MetricSample) -> str:
        """Строка выборки с экранированными значениями меток и полной точностью значения."""
        rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in sample.labels.items())
        line = f"{sample.name}{{{rendered}}} {float(sample.value)!r}"
        return line


def _escape(value: object) -> str:
    """Значение метки: обратный слеш, перевод строки и кавычка экранируются."""
    escaped = str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return escaped
