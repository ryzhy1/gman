"""Read-only inventory agent. Run periodically with a systemd timer/cron, or with --watch.

From the repository: python3 agent/collect.py ...; on hosts, the single-file build (agent/build.py):
python3 gman-agent.pyz ... (the same arguments).

Docker: python3 agent/collect.py --runtime docker --source-id server-01
K8s: python3 agent/collect.py --runtime kubernetes --source-id cluster-01 (pods, nodes, pod and node Events)
K8s GPU node, run on the node in the host PID namespace (GPU processes attributed to pods):
  python3 agent/collect.py --runtime kubernetes-node --cluster cluster-01 --node gpu-1 --source-id cluster-01.gpu-1
Default emits sanitized JSON; --send submits it to GMAN_URL (the API has no authentication).
With --send, snapshots go through a bounded on-disk spool (oldest first, retried with backoff),
and a non-blocking flock keeps runs from overlapping. --watch follows `docker events` (Docker) or the
Kubernetes watch API for pods and Events (list + watch from resourceVersion, relist on 410 Gone) and sends
a debounced snapshot after changes, so short-lived containers/pods and Docker OOM kills are reported.
Docker without --watch (timer) reads `oom` events since its previous run from `docker events --since/--until`,
so an OOM kill of a container restarted by its restart policy is reported too.
"""
import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import docker, gpu, host, kubernetes as k8s, watch
from .common import AGENT_VERSION, log
from .transport import Spool, acquire_lock, endpoint, post, send_with_retry


def collect(runtime, source_id, node="", cluster="", pods=None, events=None):
    """One snapshot. Kubernetes --watch passes `pods` from its cache and `events=[]` (it adds Events itself)."""
    observed = datetime.now(timezone.utc).isoformat()
    errors = []
    data = {"source_id": source_id, "observed_at": observed, "agent_version": AGENT_VERSION, "errors": errors}
    if runtime == "kubernetes-node":
        # GPU processes of one node; pods and nodes stay with the cluster source, so this snapshot has no instances.
        gpu_list = gpu.nvidia_gpus(errors)
        try:
            k8s.attribute(gpu_list, k8s.node_pods(node))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            # Processes still carry container ID and pod UID; only names are missing.
            errors.append("pods of the node unavailable: " + type(error).__name__)
        data.update(source_type="kubernetes-node", hostname=node, cluster=cluster, instances=[], gpus=gpu_list,
                    kernel=os.uname().release)
    elif runtime == "docker":
        data.update(source_type="docker-host", hostname=os.uname().nodename,
                    instances=[docker.docker_instance_with_files(c) for c in docker.inspect_containers()],
                    gpus=gpu.gpus(errors), **host.host_info())
    else:
        if pods is None:
            pods = k8s.kube_list(["get", "pods", "--all-namespaces"], "/api/v1/pods")["items"]
        node_list = k8s.kube_list(["get", "nodes"], "/api/v1/nodes")["items"]
        if events is None:
            raw = []
            for kind, path in (("Pod", k8s.EVENTS_PATH), ("Node", k8s.NODE_EVENTS_PATH)):
                try:
                    raw += k8s.kube_list(["get", "events", "--all-namespaces", "--field-selector",
                                          "involvedObject.kind=" + kind], path)["items"]
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    # E.g. RBAC without events: the inventory itself is still complete.
                    errors.append(f"kubernetes {kind.lower()} events unavailable: " + type(error).__name__)
            events = k8s.kube_events(raw, {ref["uid"]: ref for ref in map(k8s.pod_ref, pods)})
        data.update(source_type="kubernetes-cluster", hostname=source_id,
                    instances=[i for pod in pods for i in k8s.pod_instances(pod)], gpus=[],
                    nodes=k8s.nodes(node_list, pods), events=events)
    return data


# --- CLI ------------------------------------------------------------------------------------------

def default_state_dir():
    if os.environ.get("GMAN_AGENT_STATE_DIR"):
        return os.environ["GMAN_AGENT_STATE_DIR"]
    return "/var/lib/gman-agent" if os.geteuid() == 0 else str(Path.home() / ".local/state/gman-agent")


def run_once(collect_snapshot, spool, deliver):
    try:
        data = collect_snapshot()
    except Exception:
        # A failed collection is never submitted as an empty, authoritative snapshot;
        # snapshots spooled earlier are still delivered.
        spool.flush(deliver)
        raise
    spool.put(data)
    remaining = spool.flush(deliver)
    if remaining:
        raise SystemExit(f"Submission failed: {remaining} snapshot(s) kept in spool for retry")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", choices=["docker", "kubernetes", "kubernetes-node"], required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--node", default=os.environ.get("NODE_NAME", ""),
                        help="kubernetes-node: this node's name as in Kubernetes (default: NODE_NAME)")
    # Environment defaults let the systemd unit (deploy/systemd, ExecStart without these flags) run this mode.
    parser.add_argument("--cluster", default=os.environ.get("GMAN_CLUSTER", ""),
                        help="kubernetes-node: --source-id of the cluster's inventory agent (default: GMAN_CLUSTER)")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--watch", action="store_true",
                        help="follow docker events or the Kubernetes watch API (requires --send; docker, kubernetes)")
    parser.add_argument("--state-dir", help="spool and lock directory (default: GMAN_AGENT_STATE_DIR, "
                                            "/var/lib/gman-agent as root, else ~/.local/state/gman-agent)")
    parser.add_argument("--spool-max-files", type=int, default=100)
    parser.add_argument("--spool-max-bytes", type=int, default=50 * 1024 ** 2)
    parser.add_argument("--retries", type=int, default=3, help="delivery retries with exponential backoff")
    parser.add_argument("--debounce", type=float, default=2.0, help="--watch: seconds of quiet before a snapshot")
    parser.add_argument("--max-delay", type=float, default=10.0, help="--watch: max seconds an event waits")
    parser.add_argument("--interval", type=float, default=60.0, help="--watch: full snapshot at least this often")
    parser.add_argument("--watch-timeout", type=int, default=300,
                        help="--watch kubernetes: seconds before the API server ends a watch (it is resumed)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.source_id) or not args.source_id.strip("."):
        parser.error("invalid --source-id")
    if args.runtime == "kubernetes-node" and not (args.node and args.cluster):
        parser.error("--runtime kubernetes-node needs --node (or NODE_NAME) and --cluster")
    if args.watch and (args.runtime not in ("docker", "kubernetes") or not args.send):
        parser.error("--watch requires --runtime docker or kubernetes and --send")
    if not args.send:
        print(json.dumps(collect(args.runtime, args.source_id, args.node, args.cluster), indent=2))
        return
    target = endpoint()  # validate the configuration before collecting anything
    state = Path(args.state_dir or default_state_dir())
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if acquire_lock(state / f"{args.source_id}.lock") is None:
        log("another run for this source holds the lock; skipping")
        return
    spool = Spool(state / f"spool-{args.source_id}", args.spool_max_files, args.spool_max_bytes)

    def deliver(data):
        return send_with_retry(data, lambda d: post(d, target), retries=args.retries)

    def snapshot():
        return collect(args.runtime, args.source_id, args.node, args.cluster)

    if args.watch and args.runtime == "kubernetes":
        watcher = watch.KubeWatcher(time.monotonic(), args.debounce, args.max_delay, args.interval)

        def feed(resource, api_path):
            return lambda items: watch.kube_watch(resource, api_path, items, timeout=args.watch_timeout)

        watch.watch(lambda: collect(args.runtime, args.source_id, pods=watcher.pod_list(), events=[]), spool, deliver,
                    watcher, feeds=(feed("pods", "/api/v1/pods"), feed("events", k8s.EVENTS_PATH),
                                    feed("node-events", k8s.NODE_EVENTS_PATH)))
    elif args.watch:
        watcher = watch.Watcher(time.monotonic(), args.debounce, args.max_delay, args.interval)
        watch.watch(snapshot, spool, deliver, watcher)
    elif args.runtime == "docker":
        mark = state / f"docker-oom-{args.source_id}.since"
        run_once(lambda: docker.with_docker_ooms(snapshot(), mark), spool, deliver)
    else:
        run_once(snapshot, spool, deliver)


def run():
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        # Failed collections are never submitted as empty, authoritative snapshots.
        raise SystemExit("Collection/submission failed: " + type(error).__name__)
