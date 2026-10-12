#!/bin/sh
# External probe of a GMan server, for cron on another machine (not the GMan server itself): complements the
# GManWatchdog heartbeat (deploy/alerts) with checks Prometheus cannot make about its own host.
#   1. https://<host>/healthz answers 200 (DNS, network, nginx, portal certificate);
#   2. https://<host>/api/events?limit=1 answers 200 (the API and its database behind nginx);
#   3. the certificates nginx presents on 443 and on the OTLP port expire in more than GMAN_PROBE_WARN_DAYS.
# Silent on success; on failure prints the problems to stderr and exits 1 (cron mails the output).
#
#   deploy/monitoring/probe.sh gman.example.org
#   */5 * * * * GMAN_PROBE_CA=/etc/gman/ca.pem /usr/local/bin/gman-probe gman.example.org
#
# Environment:
#   GMAN_PROBE_PORT        HTTPS port of the portal (default 443).
#   GMAN_PROBE_CA          CA file to verify the server with (the pilot CA's ca.pem); default: system trust store.
#   GMAN_PROBE_WARN_DAYS   fail when a certificate expires sooner (default 21).
#   GMAN_PROBE_OTLP_PORT   port of the OTLP endpoint whose certificate is checked (default 4317; empty = skip).
#                          nginx asks for a client certificate there but still presents its own.
#   GMAN_PROBE_PING_URL    optional ping URL (healthchecks.io style): GET <url> on success, <url>/fail on failure,
#                          so a probe that stops running is noticed too.
#   GMAN_PROBE_TIMEOUT     seconds per request (default 10).
# Requires curl >= 7.75 and the openssl CLI.
set -u

host=${1:?Usage: probe.sh <host>  (DNS name of the portal certificate)}
warn_days=${GMAN_PROBE_WARN_DAYS:-21}
port=${GMAN_PROBE_PORT:-443}
otlp_port=${GMAN_PROBE_OTLP_PORT-4317}
timeout=${GMAN_PROBE_TIMEOUT:-10}
ca=${GMAN_PROBE_CA:-}
problems=""

fail() {
  problems="${problems}$1
"
}

check_url() {
  url=https://$host:$port$1
  if [ -n "$ca" ]; then set -- --cacert "$ca"; else set --; fi
  # errormsg: curl >= 7.75.
  result=$(curl -s -o /dev/null -w '%{http_code} %{errormsg}' --max-time "$timeout" "$@" "$url")
  case $result in
    "200 "*) ;;
    "000 "*) fail "$url: ${result#000 }" ;;
    *) fail "$url: HTTP $result" ;;
  esac
}

# Certificate presented on port $1: verified against the CA (when given) and checked for expiry.
check_cert() {
  if [ -n "$ca" ]; then set -- "$1" -CAfile "$ca" -verify_return_error; else set -- "$1"; fi
  tls_port=$1
  shift
  # s_client has no timeout of its own; stdin from /dev/null ends the session after the handshake.
  pem=$(openssl s_client -connect "$host:$tls_port" -servername "$host" "$@" </dev/null 2>/dev/null \
        | openssl x509 2>/dev/null)
  if [ -z "$pem" ]; then
    fail "$host:$tls_port: no TLS handshake or certificate not trusted"
    return
  fi
  if ! printf '%s\n' "$pem" | openssl x509 -noout -checkend "$((warn_days * 86400))" >/dev/null; then
    end=$(printf '%s\n' "$pem" | openssl x509 -noout -enddate | cut -d= -f2)
    fail "$host:$tls_port: certificate expires $end (less than $warn_days days)"
  fi
}

check_url /healthz
check_url '/api/events?limit=1'
check_cert "$port"
[ -z "$otlp_port" ] || check_cert "$otlp_port"

if [ -n "${GMAN_PROBE_PING_URL:-}" ]; then
  suffix=""
  [ -z "$problems" ] || suffix=/fail
  curl -fsS -o /dev/null --max-time "$timeout" --retry 2 "$GMAN_PROBE_PING_URL$suffix" \
    || fail "ping $GMAN_PROBE_PING_URL$suffix failed"
fi

if [ -n "$problems" ]; then
  printf 'GMan probe %s:\n%s' "$host" "$problems" >&2
  exit 1
fi
