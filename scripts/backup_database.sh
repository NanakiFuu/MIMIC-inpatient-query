#!/usr/bin/env bash
# Bash operational backup for the project database.
#
# Scope: a lightweight, cron/systemd-friendly logical backup of the business
# database with a lock, a JSON manifest, bounded retention, a log line and an
# explicit exit code. It uses pg_dump's own consistent snapshot and then checks
# the archive is readable with `pg_restore --list`.
#
# The heavier guarantees -- tying the row-count/fingerprint checks to one
# exported snapshot and restoring into an isolated verification database --
# remain in scripts/backup_database.py (--mode verify). This script does not
# replace that; it is the operational counterpart.
#
# Exit codes:  0 = backup written
#              1 = backup failed
#              2 = usage / configuration error
#              3 = another backup already holds the lock
#
# Usage: bash scripts/backup_database.sh [--retain N] [--dry-run]
set -euo pipefail
umask 077

# pg_dump uses peer authentication over the unix socket, so it must run as the
# database owner. Re-exec as that user when started with sudo. This happens
# BEFORE argument parsing so the original arguments survive the re-exec.
if [ "$(id -u)" -eq 0 ]; then
  SERVICE_USER="${MEDICAL_QUERY_SERVICE_USER:-nanaki}"
  SCRIPT_PATH="$(readlink -f -- "$0")"
  exec runuser -u "$SERVICE_USER" -- "$SCRIPT_PATH" "$@"
fi

DB="${MEDICAL_QUERY_DB:-medical_query_demo}"
RETAIN="${MEDICAL_QUERY_BACKUP_RETAIN:-7}"
BACKUP_DIR="${MEDICAL_QUERY_BACKUP_DIR:-$HOME/.local/state/medical-query/backups}"
LOG_FILE="${MEDICAL_QUERY_BACKUP_LOG:-$BACKUP_DIR/backup.log}"
PREFIX="medical-query"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --retain) RETAIN="${2:?--retain needs a value}"; shift ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo 'Usage: bash scripts/backup_database.sh [--retain N] [--dry-run]'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if ! [[ "$RETAIN" =~ ^[0-9]+$ ]] || [ "$RETAIN" -lt 2 ] || [ "$RETAIN" -gt 30 ]; then
  echo 'Retention must be an integer 2..30' >&2
  exit 2
fi
command -v pg_dump >/dev/null 2>&1 || { echo 'pg_dump not found' >&2; exit 2; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG_FILE" || true; }

# exclusive, non-blocking lock so two runs never overlap
exec 9>"$BACKUP_DIR/backup.lock"
if ! flock -n 9; then
  log "status=skipped reason=lock-held"
  echo 'Another backup holds the lock' >&2
  exit 3
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$BACKUP_DIR/${PREFIX}-${stamp}.dump"
partial="$final.partial"

if [ "$DRY_RUN" = "1" ]; then
  echo "dry-run: would write $final (retain $RETAIN)"
  log "status=dry-run target=$final"
  exit 0
fi

started="$(date +%s.%N)"
if ! pg_dump -Fc --no-owner --no-acl -d "$DB" -f "$partial"; then
  rm -f "$partial"
  log "status=failed stage=pg_dump db=$DB"
  echo 'pg_dump failed' >&2
  exit 1
fi
chmod 600 "$partial"
mv -- "$partial" "$final"

# cheap integrity check: the archive table of contents must be readable
if ! pg_restore --list "$final" >/dev/null 2>&1; then
  log "status=failed stage=pg_restore-list dump=$final"
  echo 'archive is not readable' >&2
  exit 1
fi

bytes="$(stat -c %s "$final")"
sha="$(sha256sum "$final" | cut -d' ' -f1)"
elapsed="$(awk -v a="$started" -v b="$(date +%s.%N)" 'BEGIN{printf "%.4f", b-a}')"

cat >"$final.json" <<JSON
{
  "scope": "Single-host logical backup via bash; pg_dump consistent snapshot",
  "backup_utc": "$stamp",
  "bytes": $bytes,
  "sha256": "$sha",
  "dump_seconds": $elapsed,
  "database": "$DB",
  "dump": "$final",
  "retention_count": $RETAIN
}
JSON
chmod 600 "$final.json"

# bounded retention: only this script's own prefix, newest kept
mapfile -t expired < <(ls -1t "$BACKUP_DIR"/${PREFIX}-*.dump 2>/dev/null | tail -n +$((RETAIN + 1)))
removed=0
for path in "${expired[@]}"; do
  [ -n "$path" ] || continue
  case "$(readlink -f -- "$path")" in
    "$(readlink -f -- "$BACKUP_DIR")"/*) rm -f -- "$path" "$path.json"; removed=$((removed + 1)) ;;
    *) echo "refusing unsafe retention target: $path" >&2; exit 1 ;;
  esac
done

log "status=ok dump=$final bytes=$bytes sha256=$sha seconds=$elapsed removed=$removed"
printf 'ok dump=%s bytes=%s seconds=%s removed=%s\n' "$final" "$bytes" "$elapsed" "$removed"
exit 0
