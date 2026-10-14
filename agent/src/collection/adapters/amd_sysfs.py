"""GPU AMD из sysfs (amdgpu): загрузка, память, датчики hwmon по меткам."""
from pathlib import Path

from collection.entity import JsonObject

AMD_VENDOR_ID = "0x1002"
AMD = "AMD"
CARD_PREFIX_LENGTH = 4
MILLI = 1000
MICRO = 1e6
MISSING_SENSOR = "missing"


class AmdSysfs:
    """Читает карты /sys/class/drm/card*: только AMD (vendor 0x1002)."""

    def __init__(self, drm_root: Path) -> None:
        """Запоминает каталог DRM."""
        self._drm_root = drm_root

    def gpus(self) -> list[JsonObject]:
        """Документы GPU AMD; нечитаемые карты пропускаются."""
        result = []
        for card in self._drm_root.glob("card[0-9]*"):
            if not card.name[CARD_PREFIX_LENGTH:].isdigit():
                continue
            gpu = _card(card / "device")
            if gpu is not None:
                result.append(gpu)
        return result


def _card(device: Path) -> JsonObject | None:
    """Документ GPU устройства или None, если это не AMD или устройство не читается."""
    try:
        if (device / "vendor").read_text().strip() != AMD_VENDOR_ID:
            return None
        model = (device / "device").read_text().strip()
    except OSError:
        return None
    sensors = list((device / "hwmon").glob("hwmon*"))
    sensor = sensors[0] if sensors else device / MISSING_SENSOR
    address = device.resolve().name
    gpu = {"id": address, "vendor": AMD, "model": model, "pci_bus_id": address,
           "utilization_percent": _number(device / "gpu_busy_percent"),
           "memory_utilization_percent": _number(device / "mem_busy_percent"),
           "memory_total_bytes": _number(device / "mem_info_vram_total"),
           "memory_used_bytes": _number(device / "mem_info_vram_used"),
           "temperature_c": _scaled(_hwmon(sensor, "temp", "edge"), MILLI),
           "hotspot_temperature_c": _scaled(_hwmon(sensor, "temp", "junction"), MILLI),
           "memory_temperature_c": _scaled(_hwmon(sensor, "temp", "mem"), MILLI),
           # power1_input на APU — мощность всего корпуса (CPU+GPU), поэтому берется только среднее платы.
           "power_w": _scaled(_number(sensor / "power1_average"), MICRO),
           "power_limit_w": _scaled(_number(sensor / "power1_cap"), MICRO),
           "sm_clock_mhz": _scaled(_hwmon(sensor, "freq", "sclk"), MICRO),
           "memory_clock_mhz": _scaled(_hwmon(sensor, "freq", "mclk"), MICRO),
           "fan_rpm": _number(sensor / "fan1_input")}
    return gpu


def _number(path: Path) -> float | None:
    """Число из файла sysfs или None."""
    try:
        value = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return value


def _hwmon(sensor: Path, prefix: str, label: str) -> float | None:
    """Канал hwmon по метке (edge/junction/mem, sclk/mclk), а не по номеру."""
    for path in sensor.glob(prefix + "*_label"):
        try:
            matches = path.read_text().strip() == label
        except OSError:
            continue
        if matches:
            value = _number(path.with_name(path.name.replace("_label", "_input")))
            return value
    return None


def _scaled(value: float | None, factor: float) -> float | None:
    """Значение в нужных единицах."""
    if value is None:
        return None
    scaled = value / factor
    return scaled
