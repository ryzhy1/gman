#!/bin/sh
# PostgreSQL backups for GMan: pg_dump in custom format with count-based rotation.
# Runs as the `backup` service of deploy/compose.prod.yaml (postgres image, POSIX sh).
# Connection comes from libpq env: PGHOST, PGUSER, PGDATABASE, PGPASSWORD.
#
#   pg_backup.sh            loop: back up now, then every BACKUP_INTERVAL_SECONDS
#   pg_backup.sh --once     single backup (cron/systemd or before an upgrade)
#   pg_backup.sh --check    healthcheck: newest backup is younger than 2 intervals
#
# BACKUP_DIR (default /backups), BACKUP_KEEP (default 14 newest files),
# BACKUP_INTERVAL_SECONDS (default 86400). Old files are removed only after a
# successful, readable dump, so a failing backup never rotates good ones away.
set -eu

dir=${BACKUP_DIR:-/backups}
keep=${BACKUP_KEEP:-14}
interval=${BACKUP_INTERVAL_SECONDS:-86400}

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) pg_backup: $*"; }

backup_once() {
    umask 077
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    partial="$dir/.gman-$stamp.dump.partial"
    target="$dir/gman-$stamp.dump"
    if ! pg_dump --format=custom --compress=6 --no-owner --no-acl --file="$partial"; then
        rm -f "$partial"
        log "pg_dump failed"
        return 1
    fi
    # A truncated or corrupt archive fails to list.
    if ! pg_restore --list "$partial" >/dev/null; then
        rm -f "$partial"
        log "archive check failed"
        return 1
    fi
    mv "$partial" "$target"
    log "wrote $target ($(wc -c <"$target" | tr -d " ") bytes)"
    # Names sort chronologically; keep the newest $keep.
    # shellcheck disable=SC2012 # names are generated above, no special characters
    ls -1 "$dir"/gman-*.dump | sort -r | tail -n +"$((keep + 1))" | while read -r old; do
        rm -f "$old"
        log "rotated $old"
    done
}

check() {
    max_age_min=$((interval * 2 / 60))
    [ -n "$(find "$dir" -maxdepth 1 -name 'gman-*.dump' -mmin -"$max_age_min" | head -n 1)" ]
}

case ${1:-} in
    --once) backup_once ;;
    --check) check ;;
    "")
        while :; do
            backup_once || log "backup failed; retrying in 1 hour"
            if check; then sleep "$interval"; else sleep 3600; fi
        done
        ;;
    *) echo "usage: $0 [--once|--check]" >&2; exit 2 ;;
esac
