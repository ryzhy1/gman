#!/bin/sh
# Тесты API: scripts/test.sh [аргументы pytest]; GMAN_TEST_DATABASE_URL — прогон на одноразовой БД PostgreSQL.
set -eu
cd "$(dirname "$0")/.."
poetry install --no-root
exec poetry run pytest "$@"
