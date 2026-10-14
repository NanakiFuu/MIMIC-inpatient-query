#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/evidence/p1/docker"
exec > >(tee -a "$ROOT/evidence/p1/docker/install.txt") 2>&1
export DEBIAN_FRONTEND=noninteractive
for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
  if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
    echo 'Conflicting existing package detected; refusing automatic removal'
    exit 2
  fi
done
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
if test -n "${1:-}"; then
  test -s "$1"
  if test -s /etc/apt/keyrings/docker.asc; then
    cmp "$1" /etc/apt/keyrings/docker.asc
  else
    cp "$1" /etc/apt/keyrings/docker.asc
  fi
else
  test ! -s /etc/apt/keyrings/docker.asc || { echo 'Docker key already exists; inspect before replacement'; exit 2; }
  curl --fail --location --retry 2 --max-time 30 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
fi
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
printf '%s\n' 'Types: deb' 'URIs: https://download.docker.com/linux/ubuntu' "Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}" 'Components: stable' "Architectures: $(dpkg --print-architecture)" 'Signed-By: /etc/apt/keyrings/docker.asc' > /etc/apt/sources.list.d/docker.sources
apt-get update
apt-cache policy docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
VERSION="$(apt-cache policy docker-ce | awk '/Candidate:/ {print $2}')"
test "$VERSION" != '(none)'
apt-get install -y "docker-ce=$VERSION" "docker-ce-cli=$VERSION" containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
systemctl is-active docker
docker version
docker compose version
dpkg-query -W docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker run --rm hello-world
