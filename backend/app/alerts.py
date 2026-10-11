"""Active alerts from Alertmanager (API v2) for the portal. Read-only; silencing stays in Alertmanager."""
import os

import httpx

# Labels the portal shows; anything else (e.g. the external portal label) stays in Alertmanager.
LABELS = ("severity", "scope", "source_id", "host_id", "UUID", "instance_id", "instance_name", "namespace",
          "service_id", "environment", "reason")

# Tests replace this with a client on a mock transport.
client = httpx.Client(timeout=5)


class Unavailable(Exception):
    pass


def base_url():
    return os.getenv("ALERTMANAGER_URL", "").rstrip("/")


def active():
    """Firing alerts that are neither silenced nor inhibited, critical first, then oldest first."""
    params = {"active": "true", "silenced": "false", "inhibited": "false"}
    try:
        response = client.get(base_url() + "/api/v2/alerts", params=params)
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise Unavailable(type(error).__name__) from error
    if response.status_code != 200 or not isinstance(body, list):
        raise Unavailable(f"HTTP {response.status_code}")
    result = []
    for item in body:
        labels, annotations = item.get("labels") or {}, item.get("annotations") or {}
        result.append({"name": labels.get("alertname", ""), "starts_at": item.get("startsAt"),
                       "labels": {k: labels[k] for k in LABELS if labels.get(k)},
                       "summary": annotations.get("summary", ""), "description": annotations.get("description", ""),
                       "runbook_url": annotations.get("runbook_url", "")})
    result.sort(key=lambda a: (a["labels"].get("severity") != "critical", a["starts_at"] or "", a["name"]))
    return result
