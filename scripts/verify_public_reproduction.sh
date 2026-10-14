#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${MEDICAL_QUERY_PYTHON:-$HOME/environments/medical-query/.venv/bin/python}"
mkdir -p "$ROOT/evidence/p1/reproduction"
exec > >(tee -a "$ROOT/evidence/p1/reproduction/commands.txt") 2>&1
if test "${1:-}" != --package-only; then
  "$PYTHON" "$ROOT/scripts/download_mimic.py" --directory "$ROOT/.local/download-validation"
fi
python3 -m venv "$HOME/environments/medical-query/package-verification"
VERIFY="$HOME/environments/medical-query/package-verification/bin/python"
"$VERIFY" -m pip install -r "$ROOT/requirements.lock.txt"
"$PYTHON" -m pip wheel --no-deps --wheel-dir "$HOME/.local/state/medical-query/wheels" "$ROOT"
"$VERIFY" -m pip install --no-deps --force-reinstall "$HOME/.local/state/medical-query/wheels/mimic_inpatient_query-0.3.0-py3-none-any.whl"
cd /tmp
unset PYTHONPATH
"$VERIFY" "$ROOT/scripts/verify_package.py" > "$ROOT/evidence/p1/reproduction/package.json"
cat "$ROOT/evidence/p1/reproduction/package.json"
