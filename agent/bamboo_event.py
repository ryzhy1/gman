"""Send an explicit deployment assertion from a Bamboo deployment task.

GMAN_* variables override the defaults. Without them the script reads the variables
Bamboo exports to script tasks (bamboo_deploy_environment etc.); verify their presence
on your Bamboo version with `env | grep ^bamboo_` in a test task.

Monitoring must not break deployments: a failed submission prints a warning and exits 0
unless GMAN_REQUIRED=true.
"""
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def var(name, *bamboo, default=""):
    for key in ("GMAN_" + name, *("bamboo_" + b for b in bamboo)):
        if os.getenv(key):
            return os.environ[key]
    return default


def payload():
    status = var("DEPLOYMENT_STATUS")
    environment = var("ENVIRONMENT", "deploy_environment")
    build_id = var("BUILD_ID", "buildResultKey")
    release = var("RELEASE", "deploy_release")
    actor = var("ACTOR", "ManualBuildTriggerReason_userName")
    if not (status and environment and build_id and var("SERVICE_ID")):
        raise ValueError("GMAN_SERVICE_ID, GMAN_DEPLOYMENT_STATUS, environment and build ID are required")
    result = {
        # Stable across retries of the same step, distinct for started/succeeded/failed.
        "event_id": var("EVENT_ID", default=":".join(filter(None, ("bamboo", build_id, release, environment, status)))),
        "service_id": var("SERVICE_ID"),
        "environment": environment,
        "build_id": build_id,
        "build_url": var("BUILD_URL", "resultsUrl"),
        "release": release,
        "status": status,
        "actor": actor or "unknown",
        # A Bamboo user name means a person triggered it; otherwise the trigger is not asserted.
        "actor_kind": var("ACTOR_KIND", default="human" if actor else "unknown"),
        "commit": var("COMMIT", "planRepository_revision"),
        "image_digest": var("IMAGE_DIGEST"),
    }
    result["event_id"] = "".join(ch if ch.isalnum() or ch in "_.:-" else "-" for ch in result["event_id"])[:200]
    if var("OCCURRED_AT"):
        result["occurred_at"] = var("OCCURRED_AT")  # Otherwise the server uses its receive time.
    return result


def main():
    url = os.environ["GMAN_URL"].rstrip("/")
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1:")):
        raise ValueError("HTTPS required except for loopback")
    request = Request(url + "/api/deployments", data=json.dumps(payload()).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["GMAN_CI_TOKEN"]})
    try:
        with urlopen(request, timeout=30) as response:
            print("GMan deployment event: HTTP", response.status, response.read(500).decode(errors="replace"))
    except HTTPError as error:
        # The API's own error body contains no credentials.
        raise RuntimeError(f"HTTP {error.code} {error.read(1000).decode(errors='replace')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = f"GMan deployment event submission failed: {type(error).__name__}: {error}"
        if os.getenv("GMAN_REQUIRED", "").lower() == "true":
            raise SystemExit(message)
        print("WARNING: " + message, file=sys.stderr)
