#!/bin/bash
# run-policy-gateway-e2e.sh — 起独立 E2E 测试 Gateway 容器(policy-gw-e2e)。
#
# 与生产 policy-gw 的区别(隔离,绝不扩大生产 Gateway 权限):
#   - 策略:挂载 policy-e2e-fixture.yaml(allowlist 仅 fixture 仓库),经 POLICY_FILE 覆盖。
#   - 令牌:独立 role-tokens-e2e.json(首次运行自动生成);生产令牌对本容器无效。
#   - 镜像/上游/审计库:复用 policy-gateway:latest + github-mcp + audit-pg(同库不同策略)。
#
# 纵深防御:即便脚本层 guard 失效,本 Gateway 的 fixture-only policy 也会拒生产仓库(REPO_NOT_ALLOWED)。
# 用法: wsl -- bash /mnt/d/goai/mergepilot-os/tools/run-policy-gateway-e2e.sh
set -euo pipefail
DIR=/home/ngh/.config/mergepilot
FIX_POLICY=/mnt/d/goai/mergepilot-os/tools/policy-gateway/policy-e2e-fixture.yaml
PROBE=/mnt/d/goai/mergepilot-os/tools/policy-gateway/probe-tools.py
NAME=policy-gw-e2e

# ── 1. 测试角色令牌(独立;缺失则生成)──
mkdir -p "$DIR"
TOKENS_FILE="$DIR/role-tokens-e2e.json"
if [ ! -s "$TOKENS_FILE" ]; then
  python3 -c "import json,secrets;print(json.dumps({r:secrets.token_urlsafe(32) for r in ('reviewer','fixer','verifier','coordinator')}))" > "$TOKENS_FILE"
  chmod 600 "$TOKENS_FILE"
  echo "  生成独立测试令牌 $TOKENS_FILE"
fi
ROLE_TOKENS=$(python3 -c "import json;print(json.dumps(json.load(open('$TOKENS_FILE'))))")

# ── 2. 审计/L2 DSN(从 env 文件读,不硬编码密码)──
AUDIT_ENV="$DIR/audit-db.env"; B4_ENV="$DIR/b4-roles.env"
[ -f "$AUDIT_ENV" ] || { echo "缺 $AUDIT_ENV(先跑 run-policy-gateway.sh 建账号)"; exit 1; }
[ -f "$B4_ENV" ]    || { echo "缺 $B4_ENV"; exit 1; }
PGW_AUDIT_USER=$(grep '^PGW_AUDIT_USER=' "$AUDIT_ENV" | cut -d= -f2-)
PGW_AUDIT_PASS=$(grep '^PGW_AUDIT_PASS=' "$AUDIT_ENV" | head -1 | cut -d= -f2-)
PGW_AUDIT_DB=$(grep '^PGW_AUDIT_DB=' "$AUDIT_ENV" | cut -d= -f2-)
L2_USER=$(grep '^POLICY_GATEWAY_L2_USER=' "$B4_ENV" | cut -d= -f2-)
L2_PASS=$(grep '^POLICY_GATEWAY_L2_PASS=' "$B4_ENV" | head -1 | cut -d= -f2-)
AUDIT_DSN="postgresql://${PGW_AUDIT_USER}:${PGW_AUDIT_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
L2_DSN="postgresql://${L2_USER}:${L2_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"

# ── 3. 起容器(挂载 fixture policy + 测试令牌)──
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --network hiclab-net --restart no \
  -v "$FIX_POLICY":/app/policy-e2e-fixture.yaml:ro \
  -e POLICY_FILE=/app/policy-e2e-fixture.yaml \
  -e ROLE_TOKENS="$ROLE_TOKENS" \
  -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="$AUDIT_DSN" \
  -e L2_DSN="$L2_DSN" \
  policy-gateway:latest
docker network connect mcp-backend-net "$NAME" >/dev/null 2>&1 || true
docker cp "$PROBE" "$NAME":/tmp/probe-tools.py >/dev/null

# ── 4. 等就绪(上游 github-mcp 探活)──
for i in $(seq 1 20); do docker logs "$NAME" 2>&1 | grep -qa "upstream ready" && break; sleep 1; done
docker ps --filter name="$NAME" --format "{{.Names}} | {{.Status}}"
echo "--- 启动日志(尾)---"
docker logs --tail 8 "$NAME" 2>&1 | grep -vE "^\[gateway\] DENY" || docker logs --tail 8 "$NAME" 2>&1
