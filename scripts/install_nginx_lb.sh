#!/usr/bin/env bash
# Install the local Nginx dual-instance load balancer (P2 item 1).
#
# Creates two application instances from the systemd template unit and puts
# nginx in front of them. The instances share the SAME PostgreSQL database as
# the main medical-query.service; only the listen port differs.
#
# Single WSL2 host demonstration: nginx and the database remain single points of
# failure, and passive health checks only remove a node after a real failure.
#
# Sources of truth (this script copies/substitutes, it does not duplicate):
#   deploy/systemd/medical-query@.service   -> /etc/systemd/system/
#   deploy/nginx/medical-query-lb.conf      -> /etc/nginx/sites-available/
#
# Idempotent: safe to re-run. Existing unrelated nginx sites are left alone.
# Exit codes: 0 ok / 1 install failed / 2 usage or precondition error
#
# Usage: sudo bash scripts/install_nginx_lb.sh [--instances 8865,8866] [--port 8080]
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${MEDICAL_QUERY_SERVICE_USER:-${SUDO_USER:-}}"
INSTANCES="${MEDICAL_QUERY_LB_INSTANCES:-8865,8866}"
LISTEN_PORT="${MEDICAL_QUERY_LB_PORT:-8080}"
TEMPLATE_UNIT="$ROOT/deploy/systemd/medical-query@.service"
TEMPLATE_SITE="$ROOT/deploy/nginx/medical-query-lb.conf"
UNIT=/etc/systemd/system/medical-query@.service
SITE=/etc/nginx/sites-available/medical-query-lb
EVIDENCE="$ROOT/evidence/p2/1-nginx-dual-instance"
LOG="$EVIDENCE/install.txt"

while [ $# -gt 0 ]; do
  case "$1" in
    --instances) INSTANCES="${2:?--instances needs a value}"; shift ;;
    --port) LISTEN_PORT="${2:?--port needs a value}"; shift ;;
    -h|--help) echo 'Usage: sudo bash scripts/install_nginx_lb.sh [--instances 8865,8866] [--port 8080]'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[ "$(id -u)" = "0" ] || { echo 'Run as root (wsl -d Ubuntu-24.04 -u root)' >&2; exit 2; }
[ -f "$TEMPLATE_UNIT" ] || { echo "missing template: $TEMPLATE_UNIT" >&2; exit 2; }
[ -f "$TEMPLATE_SITE" ] || { echo "missing template: $TEMPLATE_SITE" >&2; exit 2; }
[ -n "$SERVICE_USER" ] || { echo 'Cannot determine service user; pass MEDICAL_QUERY_SERVICE_USER' >&2; exit 2; }

mkdir -p "$EVIDENCE"
exec > >(tee -a "$LOG") 2>&1
printf '\n== install_nginx_lb %s ==\n' "$(date -u +%FT%TZ)"

ACCOUNT_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
GROUP="$(id -gn "$SERVICE_USER")"
PYTHON="${MEDICAL_QUERY_PYTHON:-$ACCOUNT_HOME/environments/medical-query/.venv/bin/python}"
[ -x "$PYTHON" ] || { echo "python not executable: $PYTHON" >&2; exit 2; }
case "$ROOT$PYTHON" in *[[:space:]]*) echo 'paths with whitespace are not supported' >&2; exit 2 ;; esac

# --- 1. nginx ---------------------------------------------------------------
if ! command -v nginx >/dev/null 2>&1; then
  echo 'installing nginx'
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nginx
fi
nginx -v

# --- 2. template unit, with paths substituted -------------------------------
# Replace whole directive lines so the template stays the single source of truth.
sed -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|^Group=.*|Group=$GROUP|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
    -e "s|^Environment=PYTHONPATH=.*|Environment=PYTHONPATH=$ROOT/src|" \
    -e "s|^ExecStart=.*|ExecStart=$PYTHON -m uvicorn medical_query.api:app --host 127.0.0.1 --port %i --no-access-log|" \
    "$TEMPLATE_UNIT" > "$UNIT"
systemd-analyze verify "$UNIT"
systemctl daemon-reload

# --- 3. start one instance per port ----------------------------------------
IFS=',' read -r -a ports <<< "$INSTANCES"
for port in "${ports[@]}"; do
  case "$port" in
    ''|*[!0-9]*) echo "invalid instance port: '$port'" >&2; exit 2 ;;
  esac
  systemctl enable --now "medical-query@$port"
done

# --- 4. nginx site ----------------------------------------------------------
# Rewrite the upstream server ports and the listen port in the template so the
# template file stays the single source of truth for the rest of the config.
awk -v ports="$INSTANCES" -v listen_port="$LISTEN_PORT" '
  /^upstream / { in_up = 1 }
  in_up && /server 127\.0\.0\.1:[0-9]+/ {
    n = split(ports, p, ",")
    if (idx < n) { idx++; sub(/127\.0\.0\.1:[0-9]+/, "127.0.0.1:" p[idx]) }
  }
  /^    listen 127\.0\.0\.1:[0-9]+/ { sub(/127\.0\.0\.1:[0-9]+/, "127.0.0.1:" listen_port) }
  /^}/ { in_up = 0 }
  { print }
' "$TEMPLATE_SITE" > "$SITE"

ln -sfn "$SITE" /etc/nginx/sites-enabled/medical-query-lb
# the stock default site would answer on :80 and is not part of this project
[ -e /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl enable nginx
systemctl reload nginx

# --- 5. verify --------------------------------------------------------------
sleep 3
fail=0
for port in "${ports[@]}"; do
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$port/health/ready" || true)"
  echo "instance $port -> ${code:-000}"
  [ "$code" = "200" ] || fail=1
done
code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$LISTEN_PORT/health/ready" || true)"
echo "load balancer $LISTEN_PORT -> ${code:-000}"
[ "$code" = "200" ] || fail=1

# nginx runs one round-robin counter PER WORKER (worker_processes auto), and the
# kernel decides which worker accepts each connection, so the split is NOT
# deterministic: both instances receive traffic, but the ratio varies (20/20 and
# 28/12 have both been observed, and short bursts can land on one instance).
# The real failover evidence is the "8865, 8866" / "502, 200" pair produced by
# the injected-failure experiment, not this ratio.
echo 'upstream distribution over 40 concurrent requests (informational; ratio varies):'
seq 1 40 | xargs -P8 -I{} curl -s -D - -o /dev/null "http://127.0.0.1:$LISTEN_PORT/health/live" \
  | grep -o '886[0-9]' | sort | uniq -c || true

systemctl is-active nginx "medical-query@${ports[0]}" "medical-query@${ports[1]:-${ports[0]}}"

[ "$fail" = "0" ] || { echo 'INSTALL INCOMPLETE: some endpoints are not 200' >&2; exit 1; }
echo "OK: nginx :$LISTEN_PORT -> ${INSTANCES} (shared database)"
