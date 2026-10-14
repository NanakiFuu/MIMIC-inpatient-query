#!/usr/bin/env bash
set -euo pipefail
# Configure (or roll back) the Docker daemon proxy for this WSL host only.
#
# The Docker daemon does not inherit the interactive shell proxy, so registry
# pulls need their own systemd drop-in. This keeps the change scoped, backed up
# and reversible; it never touches Mihomo, the firewall or global WSL networking.
#
# Usage:
#   sudo bash scripts/configure_docker_proxy.sh http://172.30.0.1:17890
#   sudo bash scripts/configure_docker_proxy.sh --rollback
#
# Rollback also restores any pre-existing drop-in saved as *.orig.

DROPIN=/etc/systemd/system/docker.service.d/http-proxy.conf
BACKUP=/etc/systemd/system/docker.service.d/http-proxy.conf.orig

test "$(id -u)" = 0 || { echo 'Run as root (e.g. wsl -u root, or sudo)'; exit 1; }
mkdir -p /etc/systemd/system/docker.service.d

if [ "${1:-}" = --rollback ]; then
  rm -f "$DROPIN"
  if [ -e "$BACKUP" ]; then mv -f "$BACKUP" "$DROPIN"; fi
  systemctl daemon-reload
  systemctl restart docker
  echo 'Docker daemon proxy configuration removed'
  exit 0
fi

PROXY="${1:?Usage: configure_docker_proxy.sh <proxy_url>|--rollback}"
if [ -e "$DROPIN" ] && [ ! -e "$BACKUP" ]; then cp -a "$DROPIN" "$BACKUP"; fi

cat > "$DROPIN" <<EOF
[Service]
Environment="HTTP_PROXY=$PROXY"
Environment="HTTPS_PROXY=$PROXY"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF

systemctl daemon-reload
systemctl restart docker
sleep 2
echo "docker active: $(systemctl is-active docker)"
systemctl show docker -p Environment
