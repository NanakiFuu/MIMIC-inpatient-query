#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/evidence/p0/monitoring" /var/lib/medical-query/config-backups
exec > >(tee -a "$ROOT/evidence/p0/monitoring/install.txt") 2>&1
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends prometheus prometheus-node-exporter
for f in /etc/prometheus/prometheus.yml /etc/default/prometheus /etc/default/prometheus-node-exporter; do
  backup="/var/lib/medical-query/config-backups/$(basename "$f").original"
  test -e "$backup" || cp "$f" "$backup"
done
cp "$ROOT/deploy/prometheus/prometheus.yml" /etc/prometheus/prometheus.yml
cp "$ROOT/deploy/prometheus/alerts.yml" /etc/prometheus/medical-query-alerts.yml
printf '%s\n' 'ARGS="--web.listen-address=127.0.0.1:9090 --storage.tsdb.retention.time=7d --storage.tsdb.retention.size=512MB"' > /etc/default/prometheus
printf '%s\n' 'ARGS="--web.listen-address=127.0.0.1:9100 --no-collector.textfile"' > /etc/default/prometheus-node-exporter
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/medical-query-alerts.yml
systemctl enable prometheus prometheus-node-exporter
systemctl restart prometheus prometheus-node-exporter
systemctl is-active prometheus prometheus-node-exporter
dpkg-query -W prometheus prometheus-node-exporter
prometheus --version
prometheus-node-exporter --version
