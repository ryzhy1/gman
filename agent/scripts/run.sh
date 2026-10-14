#!/bin/sh
# Локальный запуск агента из репозитория: scripts/run.sh --runtime docker --source-id server-01 [--send ...]
# Переменные окружения — см. env_example.
set -eu
exec python3 "$(dirname "$0")/../src/collect.py" "$@"
