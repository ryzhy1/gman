"""Количества ресурсов Kubernetes ("500m", "2Gi", "8") в числа."""
import re

QUANTITY = re.compile(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)")
SCALES = {"": 1, "n": 1e-9, "u": 1e-6, "m": .001, "k": 1000, "K": 1000, "M": 1e6, "G": 1e9, "T": 1e12,
          "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}
GPU_RESOURCE_SUFFIX = "/gpu"


def quantity(value: object) -> float | None:
    """Число из количества; неизвестный суффикс или неразборчивое значение — None."""
    match = QUANTITY.fullmatch(str(value))
    if not match:
        return None
    scale = SCALES.get(match[2])
    if scale is None:
        return None
    number = float(match[1]) * scale
    return number


def int_quantity(value: object) -> int | None:
    """Целое количество (байты, штуки) или None."""
    number = quantity(value)
    if number is None:
        return None
    whole = int(number)
    return whole


def gpu_count(resources: dict[str, str]) -> int:
    """Сумма ресурсов GPU (nvidia.com/gpu, amd.com/gpu, ...)."""
    total = 0
    for name, value in resources.items():
        if name.endswith(GPU_RESOURCE_SUFFIX):
            total += int_quantity(value) or 0
    return total
