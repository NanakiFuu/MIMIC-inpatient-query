#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PRIVATE_DIR="${MEDICAL_QUERY_PRIVATE_DIR:-/var/lib/medical-query/compose}"
ENV_FILE="${MEDICAL_QUERY_COMPOSE_ENV:-$PRIVATE_DIR/.env}"
cd "$ROOT"
case "${1:-health}" in
  start) sudo docker compose --env-file "$ENV_FILE" up -d ;;
  stop) sudo docker compose --env-file "$ENV_FILE" stop ;;
  health)
    sudo docker compose --env-file "$ENV_FILE" ps
    curl --fail --silent --show-error "http://127.0.0.1:${MEDICAL_QUERY_HTTP_PORT:-18765}/health/ready"
    ;;
  *) echo 'Usage: bash scripts/compose.sh start|stop|health'; exit 2 ;;
esac
