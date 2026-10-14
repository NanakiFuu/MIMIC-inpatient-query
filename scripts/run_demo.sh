#!/usr/bin/env bash
# Keep a foreground Linux process alive: systemd alone does not keep WSL awake.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${MEDICAL_QUERY_PYTHON:-$HOME/environments/medical-query/.venv/bin/python}"
"$PYTHON" scripts/smoke_http.py \
  --report .local/demo-startup-check.json
printf '\nDemo ready: http://localhost:8765/docs\nKeep this terminal open while using WSL services. Ctrl+C closes this demo session.\n'
exec sleep infinity
