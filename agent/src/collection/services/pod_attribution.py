"""Атрибуция процессов GPU узла Kubernetes: pod, контейнер и ID экземпляра inventory."""
from collection.entity import Gpu, JsonObject, PodIndex


class PodAttribution:
    """Сопоставляет процессы GPU с pod-ами по ID контейнера runtime, иначе по UID pod-а из cgroup."""

    def attribute(self, gpus: list[JsonObject], index: PodIndex) -> None:
        """Добавляет к процессам pod, контейнер и ID экземпляра ("<pod uid>:<container>")."""
        for document in gpus:
            for process in Gpu(document).processes:
                owner = index.by_container.get(process.container_id) or index.by_pod.get(process.pod_uid)
                if owner is not None:
                    process.attribute(owner)
