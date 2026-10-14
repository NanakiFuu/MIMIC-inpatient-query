#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${MEDICAL_QUERY_SERVICE_USER:-nanaki}"
USER_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
PYTHON="${MEDICAL_QUERY_PYTHON:-$USER_DIR/environments/medical-query/.venv/bin/python}"
PRIVATE_DIR="${MEDICAL_QUERY_PRIVATE_DIR:-/var/lib/medical-query/compose}"
ENV_FILE="${MEDICAL_QUERY_COMPOSE_ENV:-$PRIVATE_DIR/.env}"
export MEDICAL_QUERY_SERVICE_USER="$USER_NAME"
export MEDICAL_QUERY_PRIVATE_DIR="$PRIVATE_DIR"
# Provider credential injection is opt-in. The external reproduction path needs
# no model key, so it is not placed in the container by default.
if test "${MEDICAL_QUERY_USE_MODEL_CONFIG:-0}" = 1; then
  export MEDICAL_QUERY_MODEL_CONFIG="${MEDICAL_QUERY_MODEL_CONFIG:-$USER_DIR/.config/medical-query/model.json}"
fi
MODE="${MEDICAL_QUERY_IMPORT_MODE:-csv}"
case "${1:-}" in
  --from-source) MODE=csv ;;
  --from-dump) MODE=dump ;;
  '') ;;
  *) echo 'Usage: install_compose.sh [--from-source|--from-dump]'; exit 2 ;;
esac
mkdir -p "$ROOT/evidence/p1/docker"
exec > >(tee -a "$ROOT/evidence/p1/docker/deploy.txt") 2>&1
cd "$ROOT"
if test ! -e "$ENV_FILE"; then
  "$PYTHON" scripts/prepare_compose.py
fi
"$PYTHON" scripts/compose_bootstrap.py --mode "$MODE"
docker compose --env-file "$ENV_FILE" ps
