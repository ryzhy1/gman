#!/bin/sh
# Сборка образа GMan API: scripts/build.sh [тег]
set -eu
cd "$(dirname "$0")/.."
docker build -t "${1:-gman-api}" .
