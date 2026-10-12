"""Consistency of deploy/ files with the API (no YAML parser in the dev requirements: plain text checks)."""
import json
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def test_stand_accepts_inventory_as_large_as_production():
    stand = (DEPLOY.parent / "frontend/nginx.conf").read_text()
    prod = (DEPLOY / "nginx/gman.conf").read_text()
    for config in (stand, prod):
        block = config[config.index("location = /api/inventory"):]
        assert "client_max_body_size 20m;" in block[:block.index("}")]
    # /metrics is internal (Prometheus in the Compose network): neither proxy forwards it.
    assert "/metrics" not in stand and "location /metrics" not in prod and "location = /metrics" not in prod


def dcgm_fields():
    """Enabled fields of deploy/dcgm-counters.csv: name -> Prometheus type."""
    fields = {}
    for line in (DEPLOY / "dcgm-counters.csv").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, kind, _ = [part.strip() for part in line.split(",", 2)]
            fields[name] = kind
    return fields


def test_dcgm_names_match_counters_csv():
    # Through the OTel pipeline a DCGM counter gains the suffix _total (verified on otelcol-contrib 0.162.0);
    # gauges keep the field name. Every DCGM name used by rules, API and dashboards must be enabled.
    fields = dcgm_fields()
    sources = [DEPLOY / "alerts/rules.yaml", DEPLOY.parent / "backend/app/metrics.py",
               *sorted((DEPLOY / "grafana/dashboards").glob("*.json"))]
    # Field 112 before DCGM 3.3: the rules and the host dashboard accept both names.
    legacy = {"DCGM_FI_DEV_CLOCK_THROTTLE_REASONS": "DCGM_FI_DEV_CLOCKS_EVENT_REASONS"}
    used = set()
    for path in sources:
        for name in re.findall(r"DCGM_FI_[A-Z0-9]+(?:_[A-Z0-9]+)*(?:_total)?", path.read_text()):
            used.add(name)
            if name.endswith("_total"):
                assert fields.get(name[:-len("_total")]) == "counter", f"{path.name}: {name} is not a counter in the CSV"
            elif name in legacy:
                assert legacy[name] in fields
            else:
                assert fields.get(name) in ("gauge", "label"), f"{path.name}: {name} is not a gauge in the CSV"
    for name in ("DCGM_FI_DEV_XID_ERRORS", "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL_total", "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL_total",
                 "DCGM_FI_DEV_CLOCKS_EVENT_REASONS", "DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_MEMORY_TEMP",
                 "DCGM_FI_DEV_POWER_USAGE", "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_ROW_REMAP_FAILURE"):
        assert name in used


def test_every_alert_has_a_runbook():
    rules = set(re.findall(r"- alert: (\w+)", (DEPLOY / "alerts/rules.yaml").read_text()))
    runbooks = set(re.findall(r"\['(GMan\w+)', ", (DEPLOY.parent / "frontend/src/alerts.tsx").read_text()))
    assert rules and rules <= runbooks, rules - runbooks


def test_portal_grafana_links_match_dashboard_variables():
    # frontend/src: grafanaLink(<base>, '<uid>', {<variables>}, range).
    variables = {}
    for path in (DEPLOY / "grafana/dashboards").glob("*.json"):
        board = json.loads(path.read_text())
        variables[board["uid"]] = {v["name"] for v in board["templating"]["list"]}
    calls = []
    for path in (DEPLOY.parent / "frontend/src").glob("*.tsx"):
        calls += re.findall(r"grafanaLink\(\w+, '([\w-]+)', \{([^}]*)\}", path.read_text())
    assert {uid for uid, _ in calls} == {"gman-service", "gman-host", "gman-k8s", "gman-gpu", "gman-llm"}
    for uid, body in calls:
        names = set(re.findall(r"(\w+):", body))
        assert names and names <= variables[uid], (uid, names)


def test_kube_state_metrics_allowlist_covers_rules_dashboards_and_portal():
    manifest = (DEPLOY / "kube-state-metrics.yaml").read_text()
    allow = set(re.findall(r"kube_\w+", manifest[manifest.index("--metric-allowlist"):manifest.index("--metric-labels")]))
    used = set()
    for path in [DEPLOY / "alerts/rules.yaml", DEPLOY.parent / "backend/app/workloads.py",
                 *(DEPLOY / "grafana/dashboards").glob("*.json")]:
        # Not the gman:kube_* recording rules.
        used |= set(re.findall(r"(?<![:\w])kube_[a-z_]+", path.read_text()))
    assert used and used <= allow, used - allow

def test_otlp_identity_headers_match_between_nginx_and_gateway():
    # nginx terminates OTLP mTLS and must overwrite exactly the headers the gateway trusts; the identity
    # processors must run before batch, which drops request metadata.
    nginx = (DEPLOY / "nginx/gman.conf").read_text()
    gateway = (DEPLOY / "otel-gateway.prod.yaml").read_text()
    otlp = nginx[nginx.index("listen 4317 ssl"):]
    for header in ("x-gman-otlp-host", "x-gman-otlp-cluster"):
        assert f"grpc_set_header {header} " in otlp and f"from_context: metadata.{header}," in gateway
    assert "ssl_verify_client on;" in otlp and "ssl_crl /etc/nginx/tls/crl.pem;" in otlp
    assert "include_metadata: true" in gateway
    assert "processors: [memory_limiter, resource/identity, filter/identity, resource/cluster, batch]" in gateway
    # The gateway itself is not published in production: only nginx's 4317 is.
    prod = (DEPLOY / "compose.prod.yaml").read_text()
    otel = prod[prod.index("\n  otel:"):prod.index("\n  prometheus:")]
    assert "ports: !reset []" in otel


def test_watched_tls_files_are_mounted_into_the_api_without_keys():
    prod = (DEPLOY / "compose.prod.yaml").read_text()
    api = prod[prod.index("\n  api:"):prod.index("\n  portal:")]
    watched = re.search(r"GMAN_TLS_WATCH_FILES: \$\{GMAN_TLS_WATCH_FILES:-([^}]+)\}", api).group(1).split(",")
    mounted = re.findall(r"- \$\{GMAN_TLS_DIR:-\./pki/out\}/(\S+):(\S+):ro", api)
    assert sorted(target for _, target in mounted) == sorted(watched)
    # The CRL nginx checks OTLP clients against is among them; private keys never reach the API container.
    assert "/etc/gman/tls/crl.pem" in watched and "ssl_crl /etc/nginx/tls/crl.pem;" in (DEPLOY / "nginx/gman.conf").read_text()
    assert not any("key" in source or "key" in target for source, target in mounted)


def test_node_collectors_queue_on_disk():
    # Persistent sending queue (file_storage): the extension is enabled, the exporter's queue uses it, compaction
    # writes into the same writable directory (its default /var/lib/otelcol is not writable) and retries do not
    # give up after the default 5 minutes. Verified with otelcol-contrib 0.162.0 (README, «Очередь Collector на диске»).
    agent = (DEPLOY / "otel-agent.example.yaml").read_text()
    k8s = (DEPLOY / "otel-k8s.yaml").read_text()
    for config, directory in ((agent, "${env:GMAN_OTEL_STORAGE_DIR:-/var/lib/gman-otelcol}"), (k8s, "/var/lib/gman-otel")):
        assert config.count(f"directory: {directory}") == 2
        assert "extensions: [file_storage]" in config
        assert re.search(r"sending_queue:\n\s+storage: file_storage\n\s+sizer: bytes\n\s+queue_size: \d+", config)
        assert "max_elapsed_time: 0" in config
    unit = (DEPLOY / "systemd/gman-otelcol.service").read_text()
    assert "StateDirectory=gman-otelcol" in unit and "ProtectSystem=strict" in unit
    # Kubernetes: the queue survives pod restarts on the node, owned by the Collector's uid.
    assert "hostPath: {path: /var/lib/gman-otel, type: DirectoryOrCreate}" in k8s
    assert "{name: queue, mountPath: /var/lib/gman-otel}" in k8s and "chown 10001:10001 /var/lib/gman-otel" in k8s


def test_watchdog_goes_only_to_the_heartbeat_receiver():
    rules = (DEPLOY / "alerts/rules.yaml").read_text()
    watchdog = rules[rules.index("- alert: GManWatchdog"):]
    assert "expr: vector(1)" in watchdog[:200] and "scope: heartbeat" in watchdog[:300]
    config = (DEPLOY / "alerts/alertmanager.yaml").read_text()
    routes = config[config.index("  routes:"):config.index("inhibit_rules:")]
    # First child route, no continue: the heartbeat never reaches people, and nothing else reaches the heartbeat.
    first = routes[routes.index("    - matchers:"):]
    first = first[:first.index("    - matchers:", 1)]
    assert "['alertname=\"GManWatchdog\"']" in first and "receiver: gman-heartbeat" in first
    assert "repeat_interval: 5m" in first and "continue:" not in routes
    # No inhibition rule can hide it: none targets scope heartbeat or severity none.
    assert "heartbeat" not in config[config.index("inhibit_rules:"):] and 'severity="none"' not in config
    script = (DEPLOY / "alerts/alertmanager.sh").read_text()
    assert "printf '  - name: gman-heartbeat\\n'" in script and "GMAN_ALERT_HEARTBEAT_URL" in script
    compose = (DEPLOY / "compose.yaml").read_text()
    assert "GMAN_ALERT_HEARTBEAT_URL: ${GMAN_ALERT_HEARTBEAT_URL:-}" in compose
