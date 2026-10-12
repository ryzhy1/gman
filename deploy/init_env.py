"""Optional: create a .env with random database and Grafana admin passwords instead of the defaults.

Run it before the first `docker compose up`: PostgreSQL keeps the password it was initialized with, so a new
POSTGRES_PASSWORD for an existing volume would lock the API out. Never overwrites an existing .env.
"""
import os
from pathlib import Path
import secrets

target = Path(__file__).resolve().parent.parent / ".env"
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    for key in ("POSTGRES_PASSWORD", "GRAFANA_ADMIN_PASSWORD"):
        output.write(key + "='" + secrets.token_hex(32) + "'\n")
print("Created .env with random passwords; keep it private.")
