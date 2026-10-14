#!/bin/sh
# Тесты агента: scripts/test.sh [аргументы pytest] (dev-зависимости — через Poetry).
set -eu
cd "$(dirname "$0")/.."
poetry install --no-root
exec poetry run pytest "$@"
