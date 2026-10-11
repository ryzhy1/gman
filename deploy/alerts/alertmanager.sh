#!/bin/sh
# Entrypoint of the alertmanager service (deploy/compose.yaml): appends receivers configured through the
# environment to alertmanager.yaml and starts Alertmanager. Without any receiver alerts are still visible
# in the Alertmanager UI/API and in the portal, but nothing is sent.
#   GMAN_ALERT_WEBHOOK_URL       webhook (Alertmanager JSON payload), e.g. a chat bridge or incident tool.
#   GMAN_ALERT_EMAIL_TO          comma-separated addresses; needs GMAN_ALERT_EMAIL_FROM and
#                                GMAN_ALERT_SMTP_SMARTHOST (host:port); optional GMAN_ALERT_SMTP_USERNAME,
#                                GMAN_ALERT_SMTP_PASSWORD, GMAN_ALERT_SMTP_REQUIRE_TLS (default true).
#   GMAN_ALERTMANAGER_URL        address of Alertmanager as people reach it, for links in notifications.
# Check without starting: alertmanager.sh --render <file>; then amtool check-config <file>.
set -eu
here=$(dirname "$0")

# Values are written as YAML double-quoted scalars and must be single-line.
for name in GMAN_ALERT_WEBHOOK_URL GMAN_ALERT_EMAIL_TO GMAN_ALERT_EMAIL_FROM GMAN_ALERT_SMTP_SMARTHOST \
            GMAN_ALERT_SMTP_USERNAME GMAN_ALERT_SMTP_PASSWORD; do
  eval "value=\${$name:-}"
  case $value in *"
"*) echo "alertmanager.sh: $name must be a single line" >&2; exit 1 ;; esac
done

q() {
  printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"
}

render() {
  cat "$here/alertmanager.yaml"
  printf '\nreceivers:\n  - name: gman\n'
  if [ -n "${GMAN_ALERT_WEBHOOK_URL:-}" ]; then
    printf '    webhook_configs:\n'
    printf '      - url: %s\n        send_resolved: true\n' "$(q "$GMAN_ALERT_WEBHOOK_URL")"
  fi
  if [ -n "${GMAN_ALERT_EMAIL_TO:-}" ]; then
    : "${GMAN_ALERT_EMAIL_FROM:?Set GMAN_ALERT_EMAIL_FROM for email alerts}"
    : "${GMAN_ALERT_SMTP_SMARTHOST:?Set GMAN_ALERT_SMTP_SMARTHOST (host:port) for email alerts}"
    printf '    email_configs:\n'
    printf '      - to: %s\n' "$(q "$GMAN_ALERT_EMAIL_TO")"
    printf '        from: %s\n' "$(q "$GMAN_ALERT_EMAIL_FROM")"
    printf '        smarthost: %s\n' "$(q "$GMAN_ALERT_SMTP_SMARTHOST")"
    case ${GMAN_ALERT_SMTP_REQUIRE_TLS:-true} in
      false) printf '        require_tls: false\n' ;;
      *) printf '        require_tls: true\n' ;;
    esac
    if [ -n "${GMAN_ALERT_SMTP_USERNAME:-}" ]; then
      printf '        auth_username: %s\n' "$(q "$GMAN_ALERT_SMTP_USERNAME")"
      printf '        auth_password: %s\n' "$(q "${GMAN_ALERT_SMTP_PASSWORD:-}")"
    fi
    printf '        send_resolved: true\n'
  fi
}

if [ "${1:-}" = "--render" ]; then
  render > "${2:?Usage: alertmanager.sh --render <file>}"
  exit 0
fi

# The rendered file may contain the SMTP password: readable by the Alertmanager user only.
umask 077
config=/alertmanager/alertmanager.yml
render > "$config"
exec /bin/alertmanager --config.file="$config" --storage.path=/alertmanager \
  --web.external-url="${GMAN_ALERTMANAGER_URL:-http://127.0.0.1:9093}" "$@"
