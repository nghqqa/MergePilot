#!/bin/bash
# run-policy-gateway.sh — 起 Policy Gateway 容器(非破坏性:gateway 与 bridge 并行跑,不动现有旁路)。
# 双网络:hiclab-net(worker/controller 侧)+ mcp-backend-net(bridge 侧,预连接,割接后生效)。
# 网络割接(把 github-mcp 从 hiclab-net/hiclaw-net 摘掉)在 m3b-cutover-isolation.sh 单独做。
# 用法: wsl -- bash /mnt/d/goai/tools/run-policy-gateway.sh
set -uo pipefail
DIR=/home/ngh/.config/mergepilot
TOKENS_FILE="$DIR/role-tokens.json"
CTRL_ENV="$DIR/controller.env"

[ -f "$TOKENS_FILE" ] || { echo "缺 $TOKENS_FILE,先跑 m3b-generate-tokens.sh"; exit 1; }
[ -f "$CTRL_ENV" ]    || { echo "缺 $CTRL_ENV"; exit 1; }

# 从 controller.env 读 PG 密码(workflow_controller.py 也从这里读)
PG_PASS=$(grep -E '^PG_PASS=' "$CTRL_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_USER=$(grep -E '^PG_USER=' "$CTRL_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=$(grep -E '^PG_DATABASE=' "$CTRL_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_USER=${PG_USER:-mergepilot}; PG_DB=${PG_DB:-mergepilot_audit}
[ -n "$PG_PASS" ] || { echo "controller.env 里没 PG_PASS"; exit 1; }

ROLE_TOKENS=$(python3 -c "import json;print(json.dumps(json.load(open('$TOKENS_FILE'))))")
AUDIT_DSN="postgresql://${PG_USER}:${PG_PASS}@audit-pg:5432/${PG_DB}"

echo "=== 1. 建 mcp-backend-net(幂等)==="
docker network create --driver bridge mcp-backend-net 2>/dev/null && echo "  created" || echo "  exists"

echo "=== 1b. github-mcp 挂 mcp-backend-net(非破坏:额外网络;割接后 bridge 仅经此网可达)==="
docker network connect mcp-backend-net github-mcp 2>/dev/null && echo "  connected" || echo "  (already on)"

echo "=== 2. 应用 m3b_policy.sql(建 mcp_calls/approvals/policy_action_outbox,幂等)==="
docker cp /mnt/d/goai/tools/audit-db/m3b_policy.sql audit-pg:/tmp/m3b_policy.sql
docker exec audit-pg psql -U "$PG_USER" -d "$PG_DB" -f /tmp/m3b_policy.sql 2>&1 | grep -iE "CREATE TABLE|CREATE INDEX|CREATE TRIGGER|CREATE FUNCTION|ERROR" | head -20

echo "=== 3. 构建 policy-gateway 镜像 ==="
docker build -t policy-gateway:latest /mnt/d/goai/tools/policy-gateway 2>&1 | tail -3

echo "=== 4. 起容器(hiclab-net;稍后再 connect mcp-backend-net)==="
docker rm -f policy-gw 2>/dev/null || true
docker run -d --name policy-gw --network hiclab-net --restart unless-stopped \
  -e ROLE_TOKENS="$ROLE_TOKENS" \
  -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="$AUDIT_DSN" \
  policy-gateway:latest

echo "=== 5. 挂 mcp-backend-net(割接后 gateway 经此网连 bridge)==="
docker network connect mcp-backend-net policy-gw 2>/dev/null && echo "  connected" || echo "  (已在或失败)"

echo "=== 等 8s 看启动日志 ==="
sleep 8
docker ps --filter name=policy-gw --format "{{.Names}} | {{.Status}}"
echo "--- logs ---"
docker logs --tail 20 policy-gw 2>&1
