#!/usr/bin/env bash
# Bash health check for the local MIMIC-Demo deployment.
#
# Checks the systemd units and the two loopback HTTP endpoints, writes one line
# per run to a log file, and returns an explicit exit code so it can be used
# from cron/systemd/monitoring without parsing text.
#
# Exit codes:  0 = all checks passed
#              1 = at least one check failed
#              2 = usage / environment error
#
# Usage: bash scripts/healthcheck.sh [--json] [--include-containers]
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# keep one log regardless of which user invokes the script
SERVICE_USER="${MEDICAL_QUERY_SERVICE_USER:-nanaki}"
if [ -d "/home/$SERVICE_USER" ]; then BASE="/home/$SERVICE_USER"; else BASE="$HOME"; fi
LOG_FILE="${MEDICAL_QUERY_HEALTH_LOG:-$BASE/.local/state/medical-query/healthcheck.log}"
APP_URL="${MEDICAL_QUERY_HEALTH_URL:-http://127.0.0.1:8765/health/ready}"
PROM_URL="${MEDICAL_QUERY_PROM_URL:-http://127.0.0.1:9090/-/ready}"
TIMEOUT="${MEDICAL_QUERY_HEALTH_TIMEOUT:-5}"
UNITS=(medical-query postgresql@16-main prometheus prometheus-node-exporter)
JSON=0
WITH_CONTAINERS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --include-containers) WITH_CONTAINERS=1 ;;
    -h|--help) echo 'Usage: bash scripts/healthcheck.sh [--json] [--include-containers]'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$(dirname -- "$LOG_FILE")"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG_FILE" || true; }

failures=0
results=()

check_unit() {
  local unit="$1" state
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  if [ "$state" = "active" ]; then
    results+=("unit:$unit=active")
  else
    results+=("unit:$unit=${state:-unknown}")
    failures=$((failures + 1))
  fi
}

check_http() {
  local name="$1" url="$2" code
  code="$(curl -s -o /dev/null -m "$TIMEOUT" -w '%{http_code}' "$url" || true)"
  if [ "$code" = "200" ]; then
    results+=("http:$name=200")
  else
    results+=("http:$name=${code:-000}")
    failures=$((failures + 1))
  fi
}

for unit in "${UNITS[@]}"; do check_unit "$unit"; done
check_http app "$APP_URL"
check_http prometheus "$PROM_URL"

if [ "$WITH_CONTAINERS" = "1" ]; then
  if command -v docker >/dev/null 2>&1; then
    running="$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)"
    healthy="$(docker ps --filter health=healthy --format '{{.Names}}' 2>/dev/null | wc -l)"
    results+=("containers:running=$running,healthy=$healthy")
    if [ "$running" -gt 0 ] && [ "$healthy" -ne "$running" ]; then
      failures=$((failures + 1))
    fi
  else
    results+=("containers:docker-not-found")
    failures=$((failures + 1))
  fi
fi

status="ok"
[ "$failures" -eq 0 ] || status="degraded"

if [ "$JSON" = "1" ]; then
  printf '{"status":"%s","failures":%d,"checks":[' "$status" "$failures"
  printf '"%s",' "${results[@]}" | sed 's/,$//'
  printf ']}\n'
else
  printf '%s failures=%d\n' "$status" "$failures"
  printf '  %s\n' "${results[@]}"
fi
log "status=$status failures=$failures checks=${results[*]}"

[ "$failures" -eq 0 ] || exit 1
exit 0
