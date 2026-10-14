"""Сборка документа снимка Docker-хоста, кластера Kubernetes или GPU-узла кластера."""
from datetime import datetime

from collection.entity import AGENT_VERSION, Instance, JsonObject
from collection.mappers.kubernetes_node_mapper import nodes
from collection.mappers.kubernetes_pod_mapper import pod_instances

DOCKER_HOST = "docker-host"
KUBERNETES_CLUSTER = "kubernetes-cluster"
KUBERNETES_NODE = "kubernetes-node"


def base_snapshot(source_id: str, observed_at: datetime, errors: list[str]) -> JsonObject:
    """Общие поля снимка; ошибки частичного сбора добавляются в тот же список."""
    snapshot = {"source_id": source_id, "observed_at": observed_at.isoformat(), "agent_version": AGENT_VERSION,
                "errors": errors}
    return snapshot


def docker_snapshot(base: JsonObject, hostname: str, instances: list[JsonObject], gpus: list[JsonObject],
                    host_facts: JsonObject) -> JsonObject:
    """Снимок Docker-хоста: контейнеры, GPU и сведения о хосте."""
    snapshot = {**base, "source_type": DOCKER_HOST, "hostname": hostname, "instances": instances, "gpus": gpus,
                **host_facts}
    return snapshot


def cluster_snapshot(base: JsonObject, pods: list[JsonObject], node_list: list[JsonObject],
                     events: list[JsonObject]) -> JsonObject:
    """Снимок кластера: экземпляры pod-ов, узлы с запросами pod-ов и Events с прошлого отчета."""
    instances = []
    for pod in pods:
        instances += pod_instances(pod)
    snapshot = {**base, "source_type": KUBERNETES_CLUSTER, "hostname": base["source_id"], "instances": instances,
                "gpus": [], "nodes": nodes(node_list, pods), "events": events}
    return snapshot


def node_snapshot(base: JsonObject, node: str, cluster: str, gpus: list[JsonObject], kernel: str) -> JsonObject:
    """Снимок GPU-узла: процессы GPU узла; pod-ы и узлы остаются у источника-кластера, экземпляров нет."""
    snapshot = {**base, "source_type": KUBERNETES_NODE, "hostname": node, "cluster": cluster, "instances": [],
                "gpus": gpus, "kernel": kernel}
    return snapshot


def instances_of(snapshot: JsonObject) -> list[Instance]:
    """Экземпляры снимка как сущности (изменения видны в документе снимка)."""
    instances = [Instance(document) for document in snapshot["instances"]]
    return instances
