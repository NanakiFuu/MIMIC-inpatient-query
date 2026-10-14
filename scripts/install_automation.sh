#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${MEDICAL_QUERY_SERVICE_USER:-nanaki}"
USER_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
PYTHON="$USER_DIR/environments/medical-query/.venv/bin/python"
mkdir -p "$ROOT/evidence/p0/automation"
exec > >(tee -a "$ROOT/evidence/p0/automation/commands.txt") 2>&1
"$PYTHON" "$ROOT/scripts/experiment_self_healing.py"
"$PYTHON" "$ROOT/scripts/install_backup_timers.py" --user "$USER_NAME"
"$PYTHON" "$ROOT/scripts/backup_database.py" --mode verify --user "$USER_NAME" --report "$ROOT/evidence/p0/automation/backup-restore.json"
systemctl show medical-query-backup.service medical-query-backup-verify.service -p Id -p Result -p ExecMainStatus
systemctl list-timers 'medical-query-*' --no-pager
runuser -u "$USER_NAME" -- bash "$ROOT/scripts/check_layer.sh" p0/automation-validation
