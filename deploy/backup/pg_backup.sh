#!/bin/sh
# PostgreSQL backups for GMan: pg_dump in custom format with count-based rotation, an optional
# off-site upload hook and a periodic restore check into a temporary database.
# Runs as the `backup` service of deploy/compose.prod.yaml (postgres image, POSIX sh).
# Connection comes from libpq env: PGHOST, PGUSER, PGDATABASE, PGPASSWORD (the user must be
# allowed to create databases for the restore check; in Compose it is the database owner).
#
#   pg_backup.sh                  loop: back up now, then every BACKUP_INTERVAL_SECONDS
#   pg_backup.sh --once           single backup (cron/systemd or before an upgrade)
#   pg_backup.sh --restore-check [file]   restore the newest (or given) dump into a temporary
#                                 database, run sanity queries, drop it
#   pg_backup.sh --check          healthcheck: newest backup is younger than 2 intervals, and, when
#                                 enabled, the last upload and restore check succeeded and are not overdue
#
# BACKUP_DIR (default /backups), BACKUP_KEEP (default 14 newest files),
# BACKUP_INTERVAL_SECONDS (default 86400). Old files are removed only after a
# successful, readable dump, so a failing backup never rotates good ones away.
# BACKUP_UPLOAD_CMD (default empty: off): shell command run after each successful dump with the
#   archive path as $1, e.g. 'cp "$1" /offsite/' (a mounted network share) or, where rclone is
#   installed, 'rclone copyto "$1" remote:gman/$(basename "$1")'. A failure is logged and fails
#   --check; the local backup and rotation proceed. The remote side keeps its own retention.
# BACKUP_RESTORE_CHECK_DAYS (default 7, 0 = off): the loop runs --restore-check after a backup
#   when the last successful check is older than this.
set -eu

dir=${BACKUP_DIR:-/backups}
keep=${BACKUP_KEEP:-14}
interval=${BACKUP_INTERVAL_SECONDS:-86400}
upload=${BACKUP_UPLOAD_CMD:-}
check_days=${BACKUP_RESTORE_CHECK_DAYS:-7}

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) pg_backup: $*"; }

newest() {
    # shellcheck disable=SC2012 # names are generated below, no special characters
    ls -1 "$dir"/gman-*.dump 2>/dev/null | sort -r | head -n 1
}

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
    upload_one "$target" || true
    # Names sort chronologically; keep the newest $keep.
    # shellcheck disable=SC2012 # names are generated above, no special characters
    ls -1 "$dir"/gman-*.dump | sort -r | tail -n +"$((keep + 1))" | while read -r old; do
        rm -f "$old"
        log "rotated $old"
    done
}

# Markers: .upload-ok / .restore-check-ok (mtime = last success), .upload-failed / .restore-check-failed.
upload_one() {
    [ -n "$upload" ] || return 0
    status=0
    sh -c "$upload" gman-upload "$1" || status=$?
    if [ "$status" = 0 ]; then
        touch "$dir/.upload-ok"
        rm -f "$dir/.upload-failed"
        log "uploaded $1"
    else
        touch "$dir/.upload-failed"
        log "upload of $1 failed (BACKUP_UPLOAD_CMD exit status $status)"
        return 1
    fi
}

# Sanity queries on the restored copy. Fails on a missing core table or schema version; a restored
# copy without any source while the live database has some means the dump is not what it should be.
SANITY="
select 'schema_version=' || (select version_num from alembic_version);
select 'services=' || count(*) from services;
select 'sources=' || count(*) from sources;
select 'instances=' || count(*) from instances;
select 'config_snapshots=' || count(*) from config_snapshots;
select 'events=' || count(*) from events;
select 'deployments=' || count(*) from deployments;
select 'latest_event=' || coalesce(max(received_at)::text, 'none') from events;
"

restore_check() {
    file=${1:-$(newest)}
    if [ -z "$file" ] || [ ! -f "$file" ]; then
        log "restore check: no backup in $dir"
        touch "$dir/.restore-check-failed"
        return 1
    fi
    db="gman_restore_check_$(date -u +%Y%m%d%H%M%S)"
    # The target database is always the temporary one: -d overrides PGDATABASE.
    if ! createdb "$db"; then
        log "restore check: createdb $db failed"
        touch "$dir/.restore-check-failed"
        return 1
    fi
    ok=1
    if ! pg_restore --no-owner --no-acl --exit-on-error -d "$db" "$file"; then
        log "restore check: pg_restore of $file failed"
        ok=0
    elif ! result=$(psql -X -q -At -v ON_ERROR_STOP=1 -d "$db" -c "$SANITY"); then
        log "restore check: sanity queries failed on $file"
        ok=0
    else
        live=$(psql -X -q -At -c "select count(*) from sources" 2>/dev/null || echo "?")
        restored=$(echo "$result" | sed -n 's/^sources=//p')
        if [ "$restored" = 0 ] && [ "$live" != 0 ] && [ "$live" != "?" ]; then
            log "restore check: $file has no sources, live database has $live"
            ok=0
        fi
        log "restore check of $file: $(echo "$result" | tr '\n' ' ')live_sources=$live"
    fi
    dropdb "$db" || log "restore check: could not drop $db, drop it manually"
    if [ "$ok" = 1 ]; then
        touch "$dir/.restore-check-ok"
        rm -f "$dir/.restore-check-failed"
        log "restore check passed"
    else
        touch "$dir/.restore-check-failed"
        return 1
    fi
}

restore_check_due() {
    [ "$check_days" -gt 0 ] || return 1
    [ -z "$(find "$dir" -maxdepth 1 -name .restore-check-ok -mmin -"$((check_days * 1440))")" ]
}

fresh() { # fresh <file> <max age, minutes>
    [ -n "$(find "$dir" -maxdepth 1 -name "$1" -mmin -"$2" | head -n 1)" ]
}

check() {
    max_age_min=$((interval * 2 / 60))
    fresh 'gman-*.dump' "$max_age_min" || { echo "no backup younger than $max_age_min min"; return 1; }
    if [ -n "$upload" ]; then
        [ ! -e "$dir/.upload-failed" ] || { echo "last upload failed"; return 1; }
        fresh .upload-ok "$max_age_min" || { echo "no successful upload in $max_age_min min"; return 1; }
    fi
    if [ "$check_days" -gt 0 ]; then
        [ ! -e "$dir/.restore-check-failed" ] || { echo "last restore check failed"; return 1; }
        # Overdue only after two periods: the first check runs after the first backup.
        fresh .restore-check-ok "$((check_days * 2880 + max_age_min))" \
            || [ -z "$(find "$dir" -maxdepth 1 -name 'gman-*.dump' -mmin +"$((check_days * 1440))" | head -n 1)" ] \
            || { echo "no successful restore check in $((check_days * 2)) days"; return 1; }
    fi
}

case ${1:-} in
    --once) backup_once ;;
    --check) check ;;
    --restore-check) restore_check "${2:-}" ;;
    "")
        while :; do
            if backup_once; then
                if restore_check_due; then restore_check || log "restore check failed"; fi
            else
                log "backup failed; retrying in 1 hour"
            fi
            if fresh 'gman-*.dump' "$((interval * 2 / 60))"; then sleep "$interval"; else sleep 3600; fi
        done
        ;;
    *) echo "usage: $0 [--once|--check|--restore-check [file]]" >&2; exit 2 ;;
esac
