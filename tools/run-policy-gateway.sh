#!/bin/bash
# run-policy-gateway.sh — 起 Policy Gateway 容器。
# B3.1:严格 fail-fast —— schema/角色初始化任何失败都非零退出,**不替换现有 gateway**。
#       所有 psql 带 ON_ERROR_STOP;init 全过才 docker rm/run。
# 双网络:hiclab-net(worker/controller 侧)+ mcp-backend-net(bridge 侧)。
# 用法: wsl -- bash /mnt/d/goai/tools/run-policy-gateway.sh
set -euo pipefail   # B3.1:-e 严格失败;init 失败即中止,不触达 rm/run
DIR=/home/ngh/.config/mergepilot
TOKENS_FILE="$DIR/role-tokens.json"
CTRL_ENV="$DIR/controller.env"

[ -f "$TOKENS_FILE" ] || { echo "缺 $TOKENS_FILE,先跑 m3b-generate-tokens.sh"; exit 1; }
[ -f "$CTRL_ENV" ]    || { echo "缺 $CTRL_ENV"; exit 1; }

PG_PASS=$(grep -E '^PG_PASS=' "$CTRL_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_USER=$(grep -E '^PG_USER=' "$CTRL_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=$(grep -E '^PG_DATABASE=' "$CTRL_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_USER=${PG_USER:-mergepilot}; PG_DB=${PG_DB:-mergepilot_audit}
[ -n "$PG_PASS" ] || { echo "controller.env 里没 PG_PASS"; exit 1; }

ROLE_TOKENS=$(python3 -c "import json;print(json.dumps(json.load(open('$TOKENS_FILE'))))")

echo "=== 1. 建 mcp-backend-net(幂等)==="
docker network create --driver bridge mcp-backend-net >/dev/null 2>&1 && echo "  created" || echo "  exists"

echo "=== 1b. github-mcp 挂 mcp-backend-net(非破坏)==="
docker network connect mcp-backend-net github-mcp >/dev/null 2>&1 && echo "  connected" || echo "  (already on)"

echo "=== 2. 应用 m3b_policy.sql(建表 + B3/B3.1 约束,ON_ERROR_STOP)==="
docker cp /mnt/d/goai/tools/audit-db/m3b_policy.sql audit-pg:/tmp/m3b_policy.sql >/dev/null
if ! docker exec audit-pg psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -f /tmp/m3b_policy.sql > /tmp/m3b_schema.out 2>&1; then
  echo "  ❌ schema 初始化失败,中止(不替换 gateway):"; cat /tmp/m3b_schema.out; exit 1
fi
grep -iE "CREATE TABLE|CREATE INDEX|CREATE TRIGGER|CREATE FUNCTION|ALTER|ERROR" /tmp/m3b_schema.out | head -20 || true
echo "  schema OK (phase/decision CHECK 已幂等补齐)"

echo "=== 2b. 收敛 Gateway 专用 INSERT-only 审计账号(始终幂等)==="
bash /mnt/d/goai/tools/m3b-create-audit-role.sh

# B3:gateway 用 policy_gateway_audit(INSERT-only),不再用 mergepilot 超管
AUDIT_ENV="$DIR/audit-db.env"
[ -f "$AUDIT_ENV" ] || { echo "  ❌ 无 audit-db.env(角色脚本应已写);中止"; exit 1; }
PGW_AUDIT_USER=$(grep '^PGW_AUDIT_USER=' "$AUDIT_ENV" | cut -d= -f2-)
PGW_AUDIT_PASS=$(grep '^PGW_AUDIT_PASS=' "$AUDIT_ENV" | head -1 | cut -d= -f2-)
PGW_AUDIT_DB=$(grep '^PGW_AUDIT_DB=' "$AUDIT_ENV" | cut -d= -f2-)
[ -n "$PGW_AUDIT_PASS" ] || { echo "  ❌ audit-db.env 无密码;中止"; exit 1; }
AUDIT_DSN="postgresql://${PGW_AUDIT_USER}:${PGW_AUDIT_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
echo "  AUDIT_DSN user=${PGW_AUDIT_USER} (INSERT-only, pass=<REDACTED>)"

echo "=== 3. 构建 policy-gateway 镜像(失败即中止,不替换容器)==="
docker build -t policy-gateway:latest /mnt/d/goai/tools/policy-gateway 2>&1 | tail -3

echo "=== 4. 起容器(init 全过后才替换)==="
docker rm -f policy-gw >/dev/null 2>&1 || true
docker run -d --name policy-gw --network hiclab-net --restart unless-stopped \
  -e ROLE_TOKENS="$ROLE_TOKENS" \
  -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="$AUDIT_DSN" \
  policy-gateway:latest

echo "=== 5. 挂 mcp-backend-net ===="
docker network connect mcp-backend-net policy-gw >/dev/null 2>&1 && echo "  connected" || echo "  (已在)"

echo "=== 等 8s 看启动日志 ==="
sleep 8
docker ps --filter name=policy-gw --format "{{.Names}} | {{.Status}}"
echo "--- logs ---"
docker logs --tail 12 policy-gw 2>&1
