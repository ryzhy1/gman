"""Метки происхождения: только разрешенные метки, которые не несут значений конфигурации."""
from collection.entity import JsonObject

ORIGIN_LABELS = {
    "com.docker.compose.project": "compose_project",
    "com.docker.compose.service": "compose_service",
    "com.docker.compose.project.working_dir": "compose_working_dir",
    "org.opencontainers.image.source": "image_source",
    "org.opencontainers.image.revision": "image_revision",
    "org.opencontainers.image.version": "image_version",
    "org.opencontainers.image.created": "image_created",
    "app.kubernetes.io/name": "app_name",
    "app.kubernetes.io/version": "app_version",
    "app.kubernetes.io/managed-by": "managed_by",
    "helm.sh/chart": "helm_chart",
}
ORIGIN_VALUE_LIMIT = 2000
# Переменные выбора GPU: сообщаются вместе с запросами GPU, в них индексы или UUID GPU.
GPU_ENV = ("NVIDIA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES")
SERVICE_LABEL = "gman.service"
ENVIRONMENT_LABEL = "gman.environment"
UNKNOWN_ENVIRONMENT = "unknown"


def origin(labels: JsonObject) -> JsonObject:
    """Подсказки о происхождении из разрешенных меток."""
    hints = {}
    for label, key in ORIGIN_LABELS.items():
        if labels.get(label):
            hints[key] = str(labels[label])[:ORIGIN_VALUE_LIMIT]
    return hints
