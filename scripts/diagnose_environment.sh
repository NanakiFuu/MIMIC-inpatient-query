#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/evidence/p0/environment"
exec > >(tee "$ROOT/evidence/p0/environment/linux.txt") 2>&1
cat /etc/wsl.conf
mount | grep binfmt
systemd-analyze cat-config binfmt.d
journalctl -u systemd-binfmt -b --no-pager
printf '\nExplicit systemd-binfmt diagnostic output:\n'
/usr/lib/systemd/systemd-binfmt || true
printf '\nProxy variables: presence only, no values:\n'
for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
  test -z "${!name:-}" || printf '%s is set\n' "$name"
done
printf '\nDirect HTTPS probe:\n'
curl -I --max-time 15 https://prometheus.io/docs/introduction/overview/
printf '\nService health:\n'
systemctl is-active medical-query postgresql@16-main prometheus prometheus-node-exporter
printf '\nListening endpoints:\n'
ss -ltn
