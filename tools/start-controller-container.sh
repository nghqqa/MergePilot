#!/bin/bash
# 构建 + 启 mergepilot-controller 独立容器(B4c-0.3:保留旧容器本体,回滚=原配置)。
# 密码从 /home/ngh/.config/mergepilot/controller.env(chmod 600)读取。
# B4c:coordinator token 从 role-tokens.json 抽取;透传 L2_MERGE_ENABLED(默认 0)。
# BUILD_CTX 可经环境变量覆盖(默认 mergepilot-os 源;故障注入测试用 broken ctx)。
#
# 严格顺序(set -euo pipefail,任一失败立即中止):
#   1. build 新镜像
#   2. wait PG ready + migration 应用检查(l2_ensure_ticket 存在)
#   3. **预检**:同镜像+同 env 跑 STARTUP_CHECK_ONLY=1 → 真 startup_assert_l2;失败 → 不动旧容器
#   4. **保留旧容器本体**:stop + rename 为 mergepilot-controller-rollback(不 rm,保原配置)
#   5. run 新容器 -d
#   6. 等 Docker health → **healthy**(不是 starting);成功 → rm backup;失败/中断 → trap rename+start 原容器
set -euo pipefail
ENV_FILE=/home/ngh/.config/mergepilot/controller.env
TOKENS_FILE=/home/ngh/.config/mergepilot/role-tokens.json
BUILD_CTX="${BUILD_CTX:-/mnt/d/goai/mergepilot-os/tools/workflow-controller}"
L2_MERGE_ENABLED="${L2_MERGE_ENABLED:-0}"
NAME=mergepilot-controller
ROLLBACK=${NAME}-rollback
NET=--network=hiclab-net
HAD_OLD=0

is_truthy(){ case "${1:-0}" in 1|true|TRUE|True|yes|YES|on|ON) return 0;; *) return 1;; esac; }

# 回滚:把 backup rename 回原名 + start(恢复**原容器及原配置**,非用新 env 重建)
restore_rollback(){
  set +e
  if docker inspect "$NAME" >/dev/null 2>&1; then docker rm -f "$NAME" >/dev/null 2>&1; fi
  if docker inspect "$ROLLBACK" >/dev/null 2>&1; then
    docker rename "$ROLLBACK" "$NAME" >/dev/null 2>&1
    docker start "$NAME" >/dev/null 2>&1
    echo "!!! 已回滚到原容器(原配置 via rename+start)"
  fi
}
cleanup(){ rc=$?; if [ "$rc" -ne 0 ] && [ "$HAD_OLD" = "1" ]; then restore_rollback; fi; exit "$rc"; }

if [ ! -f "$ENV_FILE" ]; then echo "ERROR: $ENV_FILE 不存在(chmod 600;含 ADMIN_PW/PG_PASS)"; exit 1; fi

# coordinator token(role-tokens.json 唯一源;不在 controller.env 手抄,防 --force 轮换失同步)
COORD_TOKEN=""
if [ -f "$TOKENS_FILE" ]; then
  COORD_TOKEN=$(python3 -c "import json;print(json.load(open('$TOKENS_FILE')).get('coordinator',''))" 2>/dev/null || echo "")
fi

# ENV_ARGS(preflight 与正式 run 共用同一份)
ENV_ARGS=(--env-file "$ENV_FILE"
  -e PG_HOST=audit-pg -e PG_PORT=5432
  -e PG_DATABASE=mergepilot_audit -e PG_USER=mergepilot
  -e MATRIX_HS=http://hiclaw-controller:6167
  -e GATEWAY_URL=http://policy-gw:8083
  -e COORDINATOR_TOKEN="$COORD_TOKEN"
  -e L2_MERGE_ENABLED="$L2_MERGE_ENABLED")

# ─── 1. build ───
echo "=== build controller image (ctx=$BUILD_CTX) ==="
docker build -t mergepilot-controller:latest "$BUILD_CTX" 2>&1 | tail -5

# ─── 2. PG ready + migration 检查 ───
PG_SU=$(grep -E '^PG_USER=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]"); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep -E '^PG_DATABASE=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=${PG_DB:-mergepilot_audit}
SU_PW=$(grep -E '^PG_PASS=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
echo "=== wait for audit-pg ready + migration check ==="
for i in $(seq 1 30); do
  docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break
  sleep 2
done
# B4c.1.1 #7:预检要求 B4c.1 migration 完整(l2_ensure_ticket + l2_reject_approved + l2_next_attempt_at 调度列)
if ! docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A \
     -c "SELECT (EXISTS (SELECT 1 FROM pg_proc WHERE proname='l2_ensure_ticket')
            AND EXISTS (SELECT 1 FROM pg_proc WHERE proname='l2_reject_approved')
            AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='task_runs' AND column_name='l2_next_attempt_at'));" \
     2>/dev/null | grep -q "^t$"; then
  echo "ERROR: B4c/B4c.1 migration 未应用完整(缺 l2_ensure_ticket / l2_reject_approved / l2_next_attempt_at)。依次跑 m3b-b4c0-migration.sh + 应用 m3b_b4c1.sql + m3b_b4c1_1.sql。(旧容器未动)"
  exit 1
fi

# ─── 3. 预检(替换前的真断言;失败→不动旧容器,HAD_OLD 仍 0,trap 不会误回滚)───
docker rm -f "${NAME}-preflight" >/dev/null 2>&1 || true
echo "=== preflight: startup_assert_l2 via STARTUP_CHECK_ONLY (L2_MERGE_ENABLED=$L2_MERGE_ENABLED) ==="
if ! docker run --rm $NET "${ENV_ARGS[@]}" -e STARTUP_CHECK_ONLY=1 --name "${NAME}-preflight" \
     mergepilot-controller:latest 2>&1 | tee /tmp/ctrl-preflight.log | tail -8; then
  echo "ERROR: 预检失败(startup_assert_l2 未过)—— **旧容器未动**。"
  exit 1
fi
grep -q "STARTUP_CHECK_ONLY: startup_assert passed" /tmp/ctrl-preflight.log \
  || { echo "ERROR: 预检未输出通过标记—— **旧容器未动**。"; exit 1; }

# ─── 4. 保留旧容器本体(stop + rename,不 rm)───
if docker inspect "$NAME" >/dev/null 2>&1; then
  echo "=== preserve old container → $ROLLBACK (原配置保留) ==="
  docker rm -f "$ROLLBACK" >/dev/null 2>&1 || true   # 清理残留 backup
  docker stop -t 5 "$NAME" >/dev/null 2>&1 || true
  docker rename "$NAME" "$ROLLBACK"
  HAD_OLD=1
  trap cleanup EXIT   # 装备 trap:此后任何失败/中断→恢复原容器
else
  echo "=== no existing $NAME → fresh run ==="
fi

# ─── 5. run 新容器(docker run 失败 → set -e → trap 回滚)───
docker run -d --name "$NAME" $NET --restart unless-stopped "${ENV_ARGS[@]}" mergepilot-controller:latest >/dev/null

# ─── 6. 等 Docker health → healthy(非 starting;最长 ~120s)───
echo "=== health wait (poll for healthy) ==="
FINAL=unknown
for i in $(seq 1 24); do
  ST=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || echo missing)
  if [ "$ST" = "exited" ] || [ "$ST" = "missing" ]; then FINAL="exited($ST)"; break; fi
  H=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$NAME" 2>/dev/null || echo unknown)
  case "$H" in
    healthy) FINAL=healthy; break;;
    unhealthy) FINAL=unhealthy; break;;
  esac
  sleep 5
done

if [ "$FINAL" = "healthy" ]; then
  echo "=== $NAME healthy ==="
  docker ps --filter name="$NAME" --format "{{.Names}} | {{.Status}}"
  echo "=== 日志 ==="
  docker logs "$NAME" 2>&1 | head -8
  docker rm -f "$ROLLBACK" >/dev/null 2>&1 || true   # 成功 → 丢弃 backup
  trap - EXIT   # 解除 trap
  exit 0
else
  echo "ERROR: 新容器未达 healthy(最后=$FINAL)。日志:"
  docker logs "$NAME" 2>&1 | tail -12
  exit 1   # trap 恢复原容器
fi
