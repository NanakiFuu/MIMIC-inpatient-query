#!/usr/bin/env bash
# One-command readiness check before a demonstration.
#
# Prints one line per item with [ OK ] / [FAIL] / [SKIP], then a summary and an
# explicit exit code. Read-only: starts nothing, changes nothing, prints no
# secrets (only whether the model config is present and readable).
#
# Exit codes: 0 = everything required for the core demo is ready
#             1 = at least one required item is not ready
#             2 = usage error
#
# Usage: bash scripts/demo_check.sh [--quick]
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

PASS=0
FAIL=0
SKIP=0

# Root and the normal user have different $HOME, so resolve the model config
# without hard-coding a user name: explicit override, then $HOME, then any
# other user's home that actually holds one.
model_config_path() {
  if [ -n "${MEDICAL_QUERY_MODEL_CONFIG:-}" ]; then printf '%s' "$MEDICAL_QUERY_MODEL_CONFIG"; return; fi
  local candidate="$HOME/.config/medical-query/model.json" found
  if [ -r "$candidate" ]; then printf '%s' "$candidate"; return; fi
  found="$(ls -1 /home/*/.config/medical-query/model.json 2>/dev/null | head -1)"
  printf '%s' "${found:-$candidate}"
}

# `runuser -u postgres` needs root; as the database owner plain psql works.
if [ "$(id -u)" = "0" ]; then
  db() { runuser -u postgres -- "$@"; }
else
  db() { "$@"; }
fi

ok()   { printf '  [ OK ] %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); }
skip() { printf '  [SKIP] %s\n' "$1"; SKIP=$((SKIP + 1)); }

check_unit() {
  local unit="$1" state
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  [ "$state" = "active" ] && ok "service $unit" || bad "service $unit (=$state)"
}

check_http() {
  local label="$1" url="$2" code
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" || true)"
  [ "$code" = "200" ] && ok "$label ($url)" || bad "$label ($url) -> ${code:-000}"
}

check_container() {
  local name="$1" status waited=0
  # a container that just started reports "starting" until its first successful
  # healthcheck; wait rather than failing the pre-demo check immediately
  while [ "$waited" -lt 30 ]; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
    [ "$status" = "healthy" ] && break
    [ "$status" = "missing" ] && break
    sleep 2
    waited=$((waited + 2))
  done
  [ "$status" = "healthy" ] && ok "container $name" || bad "container $name (=$status)"
}

printf '\n== 演示自检 %s ==\n\n' "$(date '+%F %T')"

printf '1) 核心服务\n'
for unit in medical-query postgresql@16-main prometheus prometheus-node-exporter; do
  check_unit "$unit"
done

printf '\n2) 应用与监控端点\n'
check_http '主应用 /health/ready' 'http://127.0.0.1:8765/health/ready'
check_http '主应用首页' 'http://127.0.0.1:8765/'
check_http 'Prometheus' 'http://127.0.0.1:9090/-/ready'

printf '\n3) 数据库\n'
if db pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  ok 'PostgreSQL 5432 可连接'
else
  bad 'PostgreSQL 5432 不可连接'
fi
if db psql -p 5432 -d medical_query_demo -tAc \
     "select count(*) from query_api.encounter_overview" >/dev/null 2>&1; then
  ok '业务库 medical_query_demo 与视图可查询'
else
  bad '业务库/视图查询失败'
fi

printf '\n4) 模型配置（不打印任何密钥）\n'
MODEL_CFG="$(model_config_path)"
if [ -r "$MODEL_CFG" ]; then
  perms="$(stat -c '%a' "$MODEL_CFG" 2>/dev/null || echo '?')"
  case "$perms" in
    600|400) ok "模型配置存在且权限私有 ($perms)" ;;
    *)       bad "模型配置权限过宽 ($perms)，应为 600" ;;
  esac
else
  bad "模型配置不可读: $MODEL_CFG"
fi

if [ "$QUICK" = "1" ]; then
  printf '\n--quick：跳过容器/负载均衡/复制拓扑检查\n'
else
  printf '\n5) 容器部署\n'
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    check_unit docker
    check_container medical-query-demo-app-1
    check_container medical-query-demo-db-1
    check_http '容器应用 /health/ready' 'http://127.0.0.1:18765/health/ready'
  else
    skip 'docker 不可用或无权限（需 root）；容器演示可跳过'
  fi

  printf '\n6) 负载均衡与双实例（P2 第 1 项）\n'
  if systemctl is-active nginx >/dev/null 2>&1; then
    ok 'nginx active'
    check_http '负载均衡 /health/ready' 'http://127.0.0.1:8080/health/ready'
    check_http '实例 8865' 'http://127.0.0.1:8865/health/ready'
    check_http '实例 8866' 'http://127.0.0.1:8866/health/ready'
  else
    skip 'nginx 未运行；如需演示负载均衡请先启动'
  fi

  printf '\n7) 复制拓扑（P2 第 2 项）\n'
  if db psql -p 5532 -tAc "select 1 from pg_stat_replication" >/dev/null 2>&1; then
    state="$(db psql -p 5532 -tAc "select state from pg_stat_replication" 2>/dev/null | tr -d ' ')"
    [ "$state" = "streaming" ] && ok '主从流复制 streaming' || bad "复制状态异常 (=$state)"
  else
    skip '复制集群未运行；如需演示主从请先启动'
  fi
fi

printf '\n== 汇总 ==\n'
printf '  OK=%d  FAIL=%d  SKIP=%d\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -eq 0 ]; then
  printf '  结论：可以开始演示\n\n'
  exit 0
fi
printf '  结论：有 %d 项未就绪，先按上面的 [FAIL] 处理\n' "$FAIL"
printf '  提示：WSL 的会话结束后 systemd 会停服务；演示前请用\n'
printf '        wsl -d Ubuntu-24.04 --exec bash %s/scripts/run_demo.sh\n' "$ROOT"
printf '        保持一个前台窗口，再重新运行本检查。\n\n'
exit 1
