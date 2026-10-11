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
