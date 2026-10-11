"""Consistency of deploy/ files with the API (no YAML parser in the dev requirements: plain text checks)."""
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def test_prod_sso_callback_defaults_to_public_host():
    # The login cookie is set on the portal's host and must return to it; https makes the session cookie Secure.
    prod = (DEPLOY / "compose.prod.yaml").read_text()
    assert ("GMAN_OIDC_REDIRECT_URI: ${GMAN_OIDC_REDIRECT_URI:-https://${GMAN_PUBLIC_HOST}/api/auth/callback}"
            in prod)


def test_stand_accepts_inventory_as_large_as_production():
    stand = (DEPLOY.parent / "frontend/nginx.conf").read_text()
    prod = (DEPLOY / "nginx/gman.conf").read_text()
    for config in (stand, prod):
        block = config[config.index("location = /api/inventory"):]
        assert "client_max_body_size 20m;" in block[:block.index("}")]
    # /metrics is internal (Prometheus in the Compose network): neither proxy forwards it.
    assert "/metrics" not in stand and "location /metrics" not in prod and "location = /metrics" not in prod


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
