"""nvidia-smi: показания GPU и процессы GPU в формате CSV."""
import csv
from dataclasses import dataclass
from typing import Any

from collection.entity import ComputeApp, JsonObject
from runtime_access.command_runner import CommandRunner

MIB = 1024 ** 2
NO_SCALE = 1
NVIDIA = "NVIDIA"
UNSUPPORTED = ("N/A", "Not Supported")
BRACKETED_PREFIX = "["
HEX_BASE = 16
THROTTLE_FIELD = "throttle_reasons"
THROTTLE_REASONS = {0x1: "gpu_idle", 0x2: "applications_clocks", 0x4: "sw_power_cap", 0x8: "hw_slowdown",
                    0x10: "sync_boost", 0x20: "sw_thermal", 0x40: "hw_thermal", 0x80: "hw_power_brake",
                    0x100: "display_clocks"}


@dataclass(frozen=True)
class QueryField:
    """Поле --query-gpu: имя запроса, тип значения, ключ документа и множитель."""

    query: str
    kind: type
    key: str
    scale: int = NO_SCALE


# Базовый набор поддерживает любой текущий драйвер.
BASE_FIELDS = (QueryField("uuid", str, "id"), QueryField("name", str, "model"),
               QueryField("pci.bus_id", str, "pci_bus_id"), QueryField("driver_version", str, "driver_version"),
               QueryField("pstate", str, "pstate"), QueryField("utilization.gpu", float, "utilization_percent"),
               QueryField("utilization.memory", float, "memory_utilization_percent"),
               QueryField("memory.total", float, "memory_total_bytes", MIB),
               QueryField("memory.used", float, "memory_used_bytes", MIB),
               QueryField("temperature.gpu", float, "temperature_c"), QueryField("power.draw", float, "power_w"))
EXTENDED_FIELDS = (QueryField("temperature.memory", float, "memory_temperature_c"),
                   QueryField("power.limit", float, "power_limit_w"), QueryField("clocks.sm", float, "sm_clock_mhz"),
                   QueryField("clocks.mem", float, "memory_clock_mhz"),
                   QueryField("clocks.max.sm", float, "max_sm_clock_mhz"),
                   QueryField("fan.speed", float, "fan_percent"),
                   QueryField("clocks_throttle_reasons.active", str, THROTTLE_FIELD),
                   QueryField("ecc.errors.uncorrected.volatile.total", int, "ecc_uncorrected_errors"),
                   QueryField("pcie.link.gen.current", int, "pcie_generation"),
                   QueryField("pcie.link.width.current", int, "pcie_width"),
                   QueryField("mig.mode.current", str, "mig_mode"),
                   QueryField("persistence_mode", str, "persistence_mode"))


class NvidiaSmi:
    """Запросы к nvidia-smi; нет драйвера — FileNotFoundError, отказ — CalledProcessError."""

    def __init__(self, runner: CommandRunner) -> None:
        """Запоминает запуск команд."""
        self._runner = runner

    def gpus(self, fields: tuple[QueryField, ...]) -> list[JsonObject]:
        """Документы GPU по полям запроса; неподдерживаемое показание — None, а не ноль."""
        names = ",".join(item.query for item in fields)
        output = self._runner.output(["nvidia-smi", "--query-gpu=" + names, "--format=csv,noheader,nounits"])
        gpus = []
        for row in csv.reader(output.splitlines()):
            gpu = _gpu_document(fields, row)
            gpus.append(gpu)
        return gpus

    def compute_apps(self) -> list[ComputeApp]:
        """Процессы, использующие GPU; строки с нечисловым PID пропускаются."""
        output = self._runner.output(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory",
                                      "--format=csv,noheader,nounits"])
        apps = []
        for gpu_uuid, pid, used in csv.reader(output.splitlines()):
            if not pid.strip().isdigit():
                continue
            memory = _value(used, float)
            app = ComputeApp(gpu_uuid=gpu_uuid.strip(), pid=int(pid),
                             used_memory_bytes=int(memory * MIB) if memory is not None else None)
            apps.append(app)
        return apps


def _gpu_document(fields: tuple[QueryField, ...], row: list[str]) -> JsonObject:
    """Документ GPU из строки CSV."""
    gpu: JsonObject = {"vendor": NVIDIA, "processes": []}
    for item, raw in zip(fields, row):
        value = _value(raw, item.kind)
        gpu[item.key] = value * item.scale if value is not None and item.scale != NO_SCALE else value
    gpu[THROTTLE_FIELD] = _throttle_names(gpu.get(THROTTLE_FIELD))
    return gpu


def _value(raw: str, kind: type) -> Any:
    """Значение поля; пустое, [N/A] и Not Supported — None."""
    raw = raw.strip()
    if not raw or raw.startswith(BRACKETED_PREFIX) or raw in UNSUPPORTED:
        return None
    if kind is str:
        return raw
    try:
        value = kind(float(raw))
    except ValueError:
        return None
    return value


def _throttle_names(mask: str | None) -> list[str]:
    """Имена активных причин троттлинга из шестнадцатеричной маски."""
    try:
        value = int(mask, HEX_BASE)
    except (TypeError, ValueError):
        return []
    names = [name for bit, name in THROTTLE_REASONS.items() if value & bit]
    return names
