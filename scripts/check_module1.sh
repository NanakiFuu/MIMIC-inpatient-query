#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$ROOT/scripts/check_layer.sh" "manual/$(date -u +%Y%m%dT%H%M%SZ)"
