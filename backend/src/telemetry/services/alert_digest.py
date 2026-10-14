"""Активные алерты для портала: без heartbeat, только показываемые метки, сначала critical, затем старые."""
from telemetry.adapters.alertmanager_client import AlertmanagerClient
from telemetry.entity import ActiveAlert, RawAlert

# Метки, которые показывает портал; остальные (например, внешняя метка портала) остаются в Alertmanager.
SHOWN_LABELS = ("severity", "scope", "source_id", "host_id", "UUID", "instance_id", "instance_name", "namespace",
                "service_id", "environment", "reason", "workload_kind", "workload", "file", "subject", "issuer")
HEARTBEAT_SCOPE = "heartbeat"
CRITICAL = "critical"


class AlertDigest:
    """Готовит список сработавших алертов для людей."""

    def __init__(self, alertmanager: AlertmanagerClient) -> None:
        """Запоминает клиент Alertmanager."""
        self._alertmanager = alertmanager

    @property
    def enabled(self) -> bool:
        """Настроен ли Alertmanager."""
        enabled = self._alertmanager.enabled
        return enabled

    def active(self) -> list[ActiveAlert]:
        """Активные алерты Alertmanager, подготовленные для портала."""
        raw_alerts = self._alertmanager.active_alerts()
        alerts = self.summarize(raw_alerts)
        return alerts

    def summarize(self, raw_alerts: list[RawAlert]) -> list[ActiveAlert]:
        """Алерты без dead man's switch, упорядоченные по важности и времени начала."""
        result = []
        for raw in raw_alerts:
            # GManWatchdog срабатывает всегда: он для внешней проверки heartbeat, а не для людей.
            if raw.labels.get("scope") == HEARTBEAT_SCOPE:
                continue
            alert = _active_alert(raw)
            result.append(alert)
        result.sort(key=_priority)
        return result


def _active_alert(raw: RawAlert) -> ActiveAlert:
    """Алерт с показываемыми метками и аннотациями."""
    labels = {key: raw.labels[key] for key in SHOWN_LABELS if raw.labels.get(key)}
    annotations = raw.annotations
    alert = ActiveAlert(name=raw.labels.get("alertname", ""), starts_at=raw.starts_at, labels=labels,
                        summary=annotations.get("summary", ""), description=annotations.get("description", ""),
                        runbook_url=annotations.get("runbook_url", ""))
    return alert


def _priority(alert: ActiveAlert) -> tuple[bool, str, str]:
    """Ключ сортировки: critical раньше, затем по времени начала и имени."""
    priority = (alert.labels.get("severity") != CRITICAL, alert.starts_at or "", alert.name)
    return priority
