#!/bin/bash
# deploy/agentteams-beta/recover-runtime.sh — Wave 3.13 Controller 重启恢复工具。
#
# 目标：controller 容器重启/recreate 后，无需人工逐容器操作即可恢复四 Agent 服务。
# 已知故障形态（3.12 彩排实证）：ctrl netns 重建 → 未重启的 worker 滞留死 netns
# （127.0.0.1:9000/6167 拒绝连接）+ copaw 桥接不自愈（Re-bridge failed: get_soul）。
#
# 纪律：
#  * 幂等——重复运行不创建重复 worker/hook/配置（provision 本身幂等，见 G-10）；
#  * fail-closed——任一步失败即退出非零，绝不宣称恢复成功；
#  * 不修改数据库 job/run 状态；不输出 token/密码/配置正文/任务正文；
#  * 不使用固定容器 ID（全部经 $STACK 命名约定定位）；
#  * 默认拒绝在存在非终态 MergePilot run 时操作（--emergency 显式越过）。
#
# 用法：
#   ATB_STACK=agentteams-beta ATB_API_PORT=28462 ATB_CONSOLE_PG=agentteams-beta-pg \
#     bash recover-runtime.sh [--emergency]
# 依赖：docker、curl；ATB_ADMIN_PASSWORD/ATB_LLM_KEY 等凭据经环境注入（provision 需要）。
set -u
STACK=${ATB_STACK:-agentteams-beta}
CTRL=$STACK-ctrl
PORT=${ATB_API_PORT:-28462}
PGC=${ATB_CONSOLE_PG:-$STACK-pg}
BRIDGE_TIMEOUT=${ATB_BRIDGE_TIMEOUT:-120}
ROLES="leader reviewer fixer verifier"
EMERGENCY=0
[ "${1:-}" = "--emergency" ] && EMERGENCY=1

log() { echo "[$(date -Is)] $*"; }
fail() { log "RECOVERY_FAILED reason=$1"; exit 1; }

log "recovery start stack=$STACK ctrl=$CTRL"

# 1) MergePilot 非终态 run 检查（安全闸：恢复动作不与进行中任务竞争）
ROWS=$(docker exec "$PGC" psql -U postgres -d mu -t -A -c \
  "SELECT count(*) FROM mu.review_run WHERE status IN ('RECEIVED','REVIEW_QUEUED','REVIEWING','FIX_QUEUED','FIXING','VERIFY_QUEUED','VERIFYING')" 2>/dev/null) \
  || fail AT_PG_NOT_READY
[ "$ROWS" = "0" ] || [ "$EMERGENCY" = "1" ] || fail "AT_RUNS_ACTIVE count=$ROWS"

# 2) Controller 容器健康（API healthz）
for i in $(seq 1 20); do
  CODE=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null || echo 000)
  [ "$CODE" = "200" ] && break
  [ "$i" = "20" ] && fail AT_CTRL_NOT_READY
  sleep 3
done
log "ctrl healthy"

# 3) worker netns 重挂：ctrl netns 重建后，滞留死 netns 的 worker 一律重启重进
#   （判定=worker 视角 127.0.0.1:9000 可达性；重启即重进 ctrl 新 netns）
for ROLE in $ROLES; do
  CTR=$STACK-worker-mergepilot-$ROLE
  OK=$(docker exec $CTR sh -c 'mc cat "agentteams/agentteams-storage/agents/$AGENTTEAMS_WORKER_NAME/openclaw.json" >/dev/null 2>&1 && echo 1 || echo 0' 2>/dev/null || echo 0)
  if [ "$OK" != "1" ]; then
    log "worker $ROLE netns/MinIO 不可达 → 重启重进"
    docker restart $CTR >/dev/null
    sleep 5
  fi
done

# 4) MinIO/Matrix 可用性（经任一 worker 视角；Tuwunel 经 ctrl healthz 已含）
for i in $(seq 1 20); do
  OK=$(docker exec $STACK-worker-mergepilot-leader sh -c \
    'mc cat "agentteams/agentteams-storage/agents/$AGENTTEAMS_WORKER_NAME/openclaw.json" >/dev/null 2>&1 && echo 1 || echo 0' 2>/dev/null || echo 0)
  [ "$OK" = "1" ] && break
  [ "$i" = "20" ] && fail AT_MATRIX_NOT_READY
  sleep 3
done
log "minio/matrix reachable via worker netns"

# 5) 条件化 provision 重放——仅当有 worker 容器缺失时执行（重建路径）。
#   完整 provision 会 rm+run 每 worker 并 restart（touch 重写 MinIO → keeper 需要
#   自愈窗口）；worker 全在且 netns 已重挂时重放 provision 只引入不必要漂移。
DIR="$(cd "$(dirname "$0")" && pwd)"
MISSING=0
for ROLE in $ROLES; do
  CTR=$STACK-worker-mergepilot-$ROLE
  [ -n "$(docker ps -q --filter name=$CTR)" ] || MISSING=1
done
if [ "$MISSING" = "1" ] && [ -f "$DIR/provision-workers.sh" ]; then
  log "worker 容器缺失 → 完整 provision 重放（重建路径）"
  bash "$DIR/provision-workers.sh" >/tmp/recover-provision.log 2>&1 || fail AT_PROVISION_FAILED
  log "provision replayed (log=/tmp/recover-provision.log)"
else
  log "worker 容器全在 → 跳过 provision 重放（避免 touch/restart 竞态）"
fi

# 6) deepseek-direct 配置核对（provision touch 重写 MinIO 后，keeper watch 需最多
#    ~40s 自愈窗口——等待收敛而非立即判 drift；JSON 解析在 worker 容器内执行，
#    宿主机不要求 python3）
check_primary() {
  docker exec "$1" sh -c 'mc cat "agentteams/agentteams-storage/agents/$AGENTTEAMS_WORKER_NAME/openclaw.json" 2>/dev/null' \
    | docker exec -i "$1" python3 -c 'import json,sys
try: print(json.load(sys.stdin)["agents"]["defaults"]["model"]["primary"])
except Exception: print("PARSE_ERR")' 2>/dev/null || echo PARSE_ERR
}
for i in $(seq 1 30); do
  ALL_OK=1
  for ROLE in $ROLES; do
    P=$(check_primary $STACK-worker-mergepilot-$ROLE)
    [ "$P" = "deepseek-direct/deepseek-chat" ] || ALL_OK=0
  done
  [ "$ALL_OK" = "1" ] && break
  [ "$i" = "30" ] && fail AT_MODEL_CONFIG_DRIFT
  sleep 10
done
log "deepseek-direct config verified on 4 workers"

# 7) worker heartbeat 新鲜（copaw sync token mtime < 120s）
for ROLE in $ROLES; do
  CTR=$STACK-worker-mergepilot-$ROLE
  NOW=$(date +%s)
  MT=$(docker exec $CTR sh -c 'find /root -name "matrix_sync_token" -printf "%T@\n" 2>/dev/null | head -1 | cut -d. -f1' 2>/dev/null || echo 0)
  AGE=$((NOW - ${MT:-0}))
  [ "$AGE" -lt 120 ] || [ "$AGE" -lt 0 ] || fail AT_WORKER_HEARTBEAT_MISSING
done
log "heartbeats fresh"

# 8) Copaw bridge 消费探针（低成本：单 worker 一次最小 LLM 委派+回复）
#   ——service/heartbeat 就绪 ≠ 可消费；以真实一轮最小委托验证桥。
BRIDGE_MODE=${ATB_BRIDGE_PROBE:-1}
if [ "$BRIDGE_MODE" = "1" ] && [ -f "$DIR/../../console/backend/lib/multiuser/agents/matrix-transport.mjs" ]; then
  BE_SRC="$(cd "$DIR/../.." && pwd)"
  TOK=$(docker exec $CTRL sh -c 'tr -d "\n\r" < /var/run/agentteams/cli-token' 2>/dev/null)
  ATB_ADMIN_PASSWORD="${ATB_ADMIN_PASSWORD:?ATB_ADMIN_PASSWORD required for bridge probe}"
  MSYS_NO_PATHCONV=1 timeout 150 docker run --rm --network container:$CTRL \
    -v "$BE_SRC:/be" -v "$DIR:/probe" \
    -e AT_TOKEN="$TOK" -e ATB_ADMIN_PASSWORD \
    node:22-alpine node /probe/bridge-probe.mjs >/tmp/recover-bridge.log 2>&1 \
    || fail AT_BRIDGE_NOT_READY
  grep -q '"probe_reply": *true' /tmp/recover-bridge.log || fail AT_BRIDGE_NOT_READY
  log "bridge probe consumed a real task"
fi

# 9) 四 worker heartbeat 终验（桥探针的 LLM 轮次可能拖长 sync，放宽窗）
sleep 10
for ROLE in $ROLES; do
  CTR=$STACK-worker-mergepilot-$ROLE
  NOW=$(date +%s)
  MT=$(docker exec $CTR sh -c 'find /root -name "matrix_sync_token" -printf "%T@\n" 2>/dev/null | head -1 | cut -d. -f1' 2>/dev/null || echo 0)
  AGE=$((NOW - ${MT:-0}))
  [ "$AGE" -lt 300 ] || [ "$AGE" -lt 0 ] || fail AT_WORKER_HEARTBEAT_MISSING
done

log "RECOVERY_OK stack=$STACK"
exit 0
