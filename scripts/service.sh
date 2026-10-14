#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:-health}" in
  start) sudo systemctl start postgresql@16-main medical-query prometheus prometheus-node-exporter ;;
  stop) sudo systemctl stop medical-query ;;
  health)
    curl --fail --silent --show-error http://127.0.0.1:8765/health/ready
    curl --fail --silent --show-error http://127.0.0.1:9090/-/ready
    systemctl is-active medical-query postgresql@16-main prometheus prometheus-node-exporter
    ;;
  *) echo 'Usage: bash scripts/service.sh start|stop|health'; exit 2 ;;
esac
