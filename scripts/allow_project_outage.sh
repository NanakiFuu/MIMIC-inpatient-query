#!/usr/bin/env bash
# Temporary privilege for one explicitly requested stop/start experiment only.
set -euo pipefail
test "$(id -u)" = 0
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RULE=/etc/sudoers.d/medical-query-experiment
USER_NAME="${MEDICAL_QUERY_SERVICE_USER:-nanaki}"
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_]*$ ]]
USER_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
test ! -e "$RULE"
trap 'rm -f "$RULE"' EXIT
printf '%s\n' "$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl stop postgresql@16-main, /usr/bin/systemctl start postgresql@16-main" > "$RULE"
chmod 0440 "$RULE"
visudo -cf "$RULE"
systemctl restart medical-query.service
runuser -u "$USER_NAME" -- "$USER_DIR/environments/medical-query/.venv/bin/python" "$ROOT/scripts/experiment_monitored_outage.py"
