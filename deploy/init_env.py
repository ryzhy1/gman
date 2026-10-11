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
print("Created .env with local credentials; keep it private.")
