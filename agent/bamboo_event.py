"""Send an explicit deployment assertion from a Bamboo deployment task.

GMAN_* variables override the defaults. Without them the script reads the variables
Bamboo exports to script tasks: every dot becomes an underscore, so bamboo.deploy.environment
is $bamboo_deploy_environment. Documented deployment/release variables used here
(https://confluence.atlassian.com/bamboo/bamboo-variables-289277087.html):
deploy.environment, deploy.release (alias deploy.version), resultsUrl, triggerReason.key,
buildResultKey (the build the release was created from) and planRepository.revision.
ManualBuildTriggerReason.userName is documented for builds only; verify it on your Bamboo
version with `env | grep ^bamboo_` in a test task.

Monitoring must not break deployments: a failed submission prints a warning and exits 0
unless GMAN_REQUIRED=true.
"""
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def var(name, *bamboo, default=""):
    for key in ("GMAN_" + name, *("bamboo_" + b for b in bamboo)):
        if os.getenv(key):
            return os.environ[key]
    return default


def initiator():
    """(actor, actor_kind). bamboo.triggerReason.key is documented for deployments; the manual
    trigger user name only for builds, so it is trusted only when the trigger was manual."""
    if var("ACTOR"):
        return var("ACTOR"), var("ACTOR_KIND", default="human")
    trigger = var("TRIGGER_REASON", "triggerReason_key")
    user = var("BAMBOO_USER", "ManualBuildTriggerReason_userName")
    if "Scheduled" in trigger:
        return "bamboo-schedule", var("ACTOR_KIND", default="schedule")
    if user and (not trigger or "Manual" in trigger):
        return user, var("ACTOR_KIND", default="human")
    # Triggered automatically (e.g. after a successful build): a user name, if any, is the build's.
    return "unknown", var("ACTOR_KIND", default="unknown")


def deployment_id(results_url):
    """One Bamboo deployment run: deploymentResultId from the deployment results URL."""
    if var("DEPLOYMENT_ID"):
        return var("DEPLOYMENT_ID")
    found = re.search(r"[?&]deploymentResultId=(\d+)", results_url)
    return "bamboo-deploy-" + found.group(1) if found else ""


def payload():
    status = var("DEPLOYMENT_STATUS")
    environment = var("ENVIRONMENT", "deploy_environment")
    build_id = var("BUILD_ID", "buildResultKey")
    release = var("RELEASE", "deploy_release", "deploy_version")
    build_url = var("BUILD_URL", "resultsUrl")
    run = re.sub(r"[^a-zA-Z0-9_.:-]", "-", deployment_id(build_url))[:150]
    actor, actor_kind = initiator()
    if not (status and environment and build_id and var("SERVICE_ID")):
        raise ValueError("GMAN_SERVICE_ID, GMAN_DEPLOYMENT_STATUS, environment and build ID are required")
    # Stable across retries of the same step, distinct for started/succeeded/failed. With a
    # deployment result ID a redeploy of the same release is a new event, not a duplicate.
    default_id = (f"bamboo:{run}:{status}" if run
                  else ":".join(filter(None, ("bamboo", build_id, release, environment, status))))
    result = {
        "event_id": var("EVENT_ID", default=default_id),
        "deployment_id": run,
        "service_id": var("SERVICE_ID"),
        "environment": environment,
        "build_id": build_id,
        "build_url": build_url,
        "release": release,
        "version": var("VERSION"),
        "status": status,
        "actor": actor,
        "actor_kind": actor_kind,
        "commit": var("COMMIT", "planRepository_revision", "planRepository_1_revision"),
        "image": var("IMAGE"),
        "image_digest": var("IMAGE_DIGEST"),
    }
    result["event_id"] = "".join(ch if ch.isalnum() or ch in "_.:-" else "-" for ch in result["event_id"])[:200]
    if var("OCCURRED_AT"):
        result["occurred_at"] = var("OCCURRED_AT")  # Otherwise the server uses its receive time.
    return result


def main():
    url = os.environ["GMAN_URL"].rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ValueError("GMAN_URL must be an HTTP(S) URL")
    request = Request(url + "/api/deployments", data=json.dumps(payload()).encode(),
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            print("GMan deployment event: HTTP", response.status, response.read(500).decode(errors="replace"))
    except HTTPError as error:
        raise RuntimeError(f"HTTP {error.code} {error.read(1000).decode(errors='replace')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = f"GMan deployment event submission failed: {type(error).__name__}: {error}"
        if os.getenv("GMAN_REQUIRED", "").lower() == "true":
            raise SystemExit(message)
        print("WARNING: " + message, file=sys.stderr)
