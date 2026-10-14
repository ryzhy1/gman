"""Выборки состояния inventory для правил алертов (deploy/alerts/rules.yaml).

Значения берутся из последнего принятого снимка источника. Ряды экземпляров молчащего источника остаются
с последними значениями, а GManSourceSilent подавляет производные алерты; показания GPU молчащего источника,
наоборот, не выводятся, чтобы устаревшая температура не спорила с живыми данными DCGM.

Метки как в конвейере телеметрии: host_id — source_id Docker-хоста (GMAN_HOST_ID) или имя узла Kubernetes,
UUID — UUID NVIDIA GPU (как в DCGM Exporter).
"""
from datetime import datetime

from exposition.entity import (GpuReading, InstanceReading, MetricSample, NodeEventTime, ReleaseReading,
                               SourceReading)

# Причины, безопасные как значения меток: фиксированный набор ограничивает кардинальность и отсекает свободный текст.
STATE_REASONS = {"OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
                 "CreateContainerError", "RunContainerError", "ContainerCannotRun", "Error", "Completed"}
# Те же биты, что у clocks_throttle_reasons nvidia-smi и DCGM_FI_DEV_CLOCKS_EVENT_REASONS.
THROTTLE_BITS = {"gpu_idle": 0x1, "applications_clocks": 0x2, "sw_power_cap": 0x4, "hw_slowdown": 0x8,
                 "sync_boost": 0x10, "sw_thermal": 0x20, "hw_thermal": 0x40, "hw_power_brake": 0x80,
                 "display_clocks": 0x100}
NVIDIA = "NVIDIA"
DOCKER_HOST = "docker-host"
KUBERNETES_NODE = "kubernetes-node"
KUBERNETES = "kubernetes"
OOM_KILLED = "OOMKilled"
TERMINATION_REASON_SEPARATOR = " "


class InventorySamples:
    """Выборки источников, узлов, GPU, экземпляров, Events узлов и релизов."""

    def __init__(self, gpu_fresh_seconds: float) -> None:
        """Запоминает, сколько секунд показания GPU считаются текущими (как порог GManSourceSilent)."""
        self._gpu_fresh_seconds = gpu_fresh_seconds

    def sources(self, sources: list[SourceReading], now: datetime) -> list[MetricSample]:
        """Время и ошибки снимка, узлы кластера и свежие показания NVIDIA GPU."""
        samples = []
        for source in sources:
            host_id = _host_id(source)
            base = {"source_id": source.source_id, "source_type": source.source_type}
            stamp_labels = {**base, "host_id": host_id} if host_id else base
            samples.append(_sample("gman_source_last_snapshot_timestamp_seconds", stamp_labels,
                                   source.received_at.timestamp()))
            samples.append(_sample("gman_source_collect_errors", base, source.error_count))
            samples += _node_samples(source)
            if (now - source.received_at).total_seconds() <= self._gpu_fresh_seconds:
                samples += _gpu_samples(source, host_id or source.source_id)
        return samples

    def instances(self, instances: list[InstanceReading]) -> list[MetricSample]:
        """Перезапуски, причины состояния и последнее OOM каждого экземпляра."""
        samples = []
        for instance in instances:
            labels = _instance_labels(instance)
            samples.append(_sample("gman_instance_restarts_total", labels, instance.restarts))
            if instance.reason in STATE_REASONS:
                samples.append(_sample("gman_instance_state_reason", {**labels, "reason": instance.reason}, 1))
            last = instance.last_termination_reason.split(TERMINATION_REASON_SEPARATOR)[0]
            if last in STATE_REASONS:
                samples.append(_sample("gman_instance_last_termination_reason", {**labels, "reason": last}, 1))
            if instance.last_oom_at is not None:
                # reason="OOMKilled" дает ряду набор меток gman_instance_state_reason: GManInstanceOOMKilled
                # поднимает один алерт, когда оба сообщают об одном и том же OOM.
                oom_labels = {**labels, "reason": OOM_KILLED}
                samples.append(_sample("gman_instance_last_oom_timestamp_seconds", oom_labels,
                                       instance.last_oom_at.timestamp()))
        return samples

    def node_events(self, times: list[NodeEventTime]) -> list[MetricSample]:
        """Последнее время Event узла; host_id повторяет узел, чтобы GManK8sNodeNotReady подавлял по host_id."""
        samples = []
        for event_time in times:
            labels = {"source_id": event_time.source_id, "host_id": event_time.node, "node": event_time.node,
                      "reason": event_time.reason}
            sample = _sample("gman_k8s_node_event_last_timestamp_seconds", labels, event_time.last_at.timestamp())
            samples.append(sample)
        return samples

    def releases(self, releases: list[ReleaseReading]) -> list[MetricSample]:
        """Статус и начало последнего релиза каждой пары сервис/окружение."""
        samples = []
        for release in releases:
            labels = {"service_id": release.service_id, "environment": release.environment}
            samples.append(_sample("gman_release_status", {**labels, "status": release.status}, 1))
            samples.append(_sample("gman_release_started_timestamp_seconds", labels,
                                   release.started_at.timestamp()))
        return samples


def _sample(name: str, labels: dict[str, str], value: float) -> MetricSample:
    """Выборка метрики."""
    sample = MetricSample(name=name, labels=labels, value=value)
    return sample


def _host_id(source: SourceReading) -> str:
    """host_id источника: Docker-хост — его source_id, GPU-узел — имя узла; у кластера одного хоста нет.

    Общий host_id позволяет GManK8sNodeNotReady подавлять алерты узлового источника, а рядам DCGM —
    заменять ряды nvidia-smi в правилах записи вместо дублирования GPU.
    """
    host_ids = {DOCKER_HOST: source.source_id, KUBERNETES_NODE: source.hostname}
    host_id = host_ids.get(source.source_type, "")
    return host_id


def _node_samples(source: SourceReading) -> list[MetricSample]:
    """Готовность и давление узлов кластера."""
    samples = []
    for node in source.nodes:
        node_labels = {"source_id": source.source_id, "host_id": node.name}
        samples.append(_sample("gman_k8s_node_ready", node_labels, 1 if node.ready else 0))
        for condition in sorted(set(node.pressure)):
            samples.append(_sample("gman_k8s_node_pressure", {**node_labels, "condition": condition}, 1))
    return samples


def _gpu_samples(source: SourceReading, host_id: str) -> list[MetricSample]:
    """Температура, ECC и причины троттлинга NVIDIA GPU: метки как у DCGM, поэтому только NVIDIA."""
    samples = []
    for gpu in source.gpus:
        if gpu.vendor != NVIDIA:
            continue
        labels = {"source_id": source.source_id, "host_id": host_id, "UUID": gpu.id, "model": gpu.model}
        samples += _gpu_readings(gpu, labels)
    return samples


def _gpu_readings(gpu: GpuReading, labels: dict[str, str]) -> list[MetricSample]:
    """Выборки одного GPU; неподдерживаемые показания не выводятся."""
    samples = []
    if gpu.temperature_c is not None:
        samples.append(_sample("gman_gpu_temperature_celsius", labels, gpu.temperature_c))
    if gpu.ecc_uncorrected_errors is not None:
        samples.append(_sample("gman_gpu_ecc_uncorrected_errors", labels, gpu.ecc_uncorrected_errors))
    if gpu.throttle_reasons is not None:
        mask = sum(THROTTLE_BITS.get(reason, 0) for reason in set(gpu.throttle_reasons))
        samples.append(_sample("gman_gpu_clock_event_reasons", labels, mask))
    return samples


def _instance_labels(instance: InstanceReading) -> dict[str, str]:
    """Метки экземпляра; хост pod-а — его узел (пустой до планирования), хост контейнера — Docker-хост."""
    host = instance.node if instance.runtime == KUBERNETES else instance.source_id
    labels = {"source_id": instance.source_id, "host_id": host, "instance_id": instance.instance_id,
              "instance_name": instance.name, "namespace": instance.namespace, "service_id": instance.service_id,
              "environment": instance.environment, "runtime": instance.runtime}
    return labels
