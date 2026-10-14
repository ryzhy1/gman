#!/bin/sh
# Локальный запуск API с перезагрузкой при изменениях: scripts/run.sh (переменные — см. env_example).
set -eu
cd "$(dirname "$0")/.."
poetry install --no-root
exec poetry run uvicorn main:create_app --factory --app-dir src --reload --host 127.0.0.1 --port 8000
