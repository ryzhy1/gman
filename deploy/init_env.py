"""Create a local-only .env without printing credentials or overwriting a file."""
import json
import os
from pathlib import Path
import secrets

target = Path(__file__).resolve().parent.parent / ".env"
values = {key: secrets.token_hex(32) for key in (
    "POSTGRES_PASSWORD", "GMAN_READ_TOKEN", "GMAN_ADMIN_TOKEN", "GMAN_CI_TOKEN",
    "GRAFANA_ADMIN_PASSWORD")}
values["GMAN_AGENT_TOKENS"] = json.dumps({"server-01": secrets.token_hex(32),
                                          "cluster-01": secrets.token_hex(32)}, separators=(",", ":"))
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    for key, value in values.items():
        output.write(key + "='" + value + "'\n")
    # SSO stays off until an IdP client is registered; see README, «Вход и роли».
    output.write("# GMAN_OIDC_ISSUER='https://idp.example/realms/corp'\n"
                 "# GMAN_OIDC_CLIENT_ID='gman-portal'\n"
                 "# GMAN_OIDC_CLIENT_SECRET=''\n"
                 "# GMAN_OIDC_REDIRECT_URI='https://gman.example/api/auth/callback'\n"
                 "# GMAN_OIDC_ADMIN_GROUPS='gman-admins'\n"
                 "# GMAN_OIDC_OWNER_GROUPS='gman-service-owners'\n"
                 "# GMAN_OIDC_VIEWER_GROUPS='gman-viewers'\n")
print("Created .env with local credentials; keep it private.")
