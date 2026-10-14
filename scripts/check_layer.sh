#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${MEDICAL_QUERY_PYTHON:-$HOME/environments/medical-query/.venv/bin/python}"
NAME="${1:?Supply a new evidence directory name}"
[[ "$NAME" =~ ^[a-zA-Z0-9/-]+$ ]] || exit 2
REPORT="evidence/$NAME"
mkdir -p "$REPORT"
test ! -e "$REPORT/tests.xml" || { echo 'Refusing to overwrite tests'; exit 2; }
"$PYTHON" -m compileall -q src scripts tests
"$PYTHON" -m pip check
"$PYTHON" -m pytest -q --junitxml="$REPORT/tests.xml"
"$PYTHON" scripts/smoke_http.py --report "$REPORT/http.json"
