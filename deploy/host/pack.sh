#!/bin/sh
# Packs what another Docker host needs to connect to GMan (deploy/host/compose.yaml and what it builds) into
# gman-host.tar.gz in the current directory. On the host:
#   tar xzf gman-host.tar.gz && cd gman-host
#   cp deploy/host/.env.example deploy/host/.env   # fill in GMAN_SERVER and GMAN_SOURCE_ID
#   docker compose -f deploy/host/compose.yaml up -d --build
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
out=${1:-gman-host.tar.gz}
tar -czf "$out" -C "$root" --transform 's,^,gman-host/,' \
  agent/Dockerfile agent/.dockerignore agent/collect.py agent/bamboo_event.py \
  deploy/host/compose.yaml deploy/host/.env.example deploy/.dockerignore deploy/images/otel.Dockerfile \
  deploy/otel-node.yaml deploy/otel-gateway.yaml deploy/otel-gateway.prod.yaml
echo "Created $out"
