#!/bin/sh
# Сборка агента: gman-agent.pyz для хостов и образ для Compose. scripts/build.sh [тег образа]
set -eu
cd "$(dirname "$0")/.."
python3 scripts/build_pyz.py
docker build -t "${1:-gman-agent}" .
