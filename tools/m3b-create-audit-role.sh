#!/bin/bash
# m3b-create-audit-role.sh — 创建 Gateway 专用 INSERT-only 审计账号 policy_gateway_audit。
# 仅授 INSERT mcp_calls;显式拒 SELECT/UPDATE/DELETE/TRUNCATE;不碰 approvals/policy_action_outbox 等业务表。
# 密码写 /home/ngh/.config/mergepilot/audit-db.env(chmod 600),run-policy-gateway.sh 读取。
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-create-audit-role.sh [--force]
set -euo pipefail
DIR=/home/ngh/.config/mergepilot
ENVF="$DIR/audit-db.env"
mkdir -p "$DIR"; chmod 700 "$DIR"
FORCE=0; [ "${1:-}" = "--force" ] && FORCE=1

# 复用 mergepilot 超管建账号(从 controller.env 取)
CTRL="$DIR/controller.env"
PG_PASS=$(grep -E '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_USER=$(grep -E '^PG_USER=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=$(grep -E '^PG_DATABASE=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_USER=${PG_USER:-mergepilot}; PG_DB=${PG_DB:-mergepilot_audit}

# 已存在则不覆盖密码(除非 --force)
if [ -f "$ENVF" ] && [ "$FORCE" = "0" ]; then
  echo "已存在 $ENVF(不覆盖;如需重置加 --force,并重启 gateway)"
else
  PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
  # 非引用 heredoc:shell 展开 $PW(token_urlsafe 字符集安全,单引号字面量)。
  # 不能用 psql -v + current_setting:那是 GUC 不是 psql var;DO block 内也不插值。
  # ON_ERROR_STOP=1:DDL 报错立即终止,避免"密码已落盘但角色没建"的不一致。
  docker exec -i audit-pg psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 <<SQL
DO \$do\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_audit') THEN
    CREATE ROLE policy_gateway_audit LOGIN PASSWORD '$PW';
  ELSE
    ALTER ROLE policy_gateway_audit PASSWORD '$PW';
  END IF;
END \$do\$;
GRANT CONNECT ON DATABASE mergepilot_audit TO policy_gateway_audit;
GRANT USAGE ON SCHEMA public TO policy_gateway_audit;
REVOKE ALL ON mcp_calls FROM policy_gateway_audit;
GRANT INSERT ON mcp_calls TO policy_gateway_audit;
REVOKE SELECT, UPDATE, DELETE, TRUNCATE ON mcp_calls FROM policy_gateway_audit;
REVOKE ALL ON approvals, policy_action_outbox FROM policy_gateway_audit;
SQL
  echo "role policy_gateway_audit created/rotated (INSERT-only on mcp_calls)"
  cat > "$ENVF" <<EOF
PGW_AUDIT_USER=policy_gateway_audit
PGW_AUDIT_PASS=$PW
PGW_AUDIT_DB=$PG_DB
EOF
  chmod 600 "$ENVF"
fi

# 预览(不回显密码)
echo "=== 账号信息 ==="
echo "  user: policy_gateway_audit"
echo "  db:   $PG_DB"
echo "  pass: $(grep PASS "$ENVF" | cut -d= -f2 | head -c8)... (len=$(grep PASS "$ENVF" | cut -d= -f2 | wc -c))"

# 验证权限:INSERT 应成功,UPDATE/DELETE/TRUNCATE 应失败
echo ""
echo "=== 权限自检 ==="
PGW_PASS=$(grep PASS "$ENVF" | head -1 | cut -d= -f2)
echo -n "  INSERT: "
INS=$(docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c \
  "INSERT INTO mcp_calls(request_id,caller_agent,tool,decision,phase) VALUES('permtest-'||md5(random()::text),'selftest','(permtest)','ALLOW','INTENT');" 2>&1 | head -1)
echo "$INS" | grep -qiE "INSERT 0|ERROR|denied" && echo "$INS" | grep -qi "INSERT 0" && echo "OK ✓" || echo "FAIL: $INS"
echo -n "  SELECT: "
docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c \
  "SELECT count(*) FROM mcp_calls;" 2>&1 | grep -qiE "permission denied" && echo "DENIED ✓" || echo "!!! allowed !!!"
echo -n "  UPDATE: "
docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c \
  "UPDATE mcp_calls SET decision='X' WHERE request_id LIKE 'permtest-%';" 2>&1 | grep -qiE "permission denied" && echo "DENIED ✓" || echo "!!! allowed !!!"
echo -n "  DELETE: "
docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c \
  "DELETE FROM mcp_calls WHERE request_id LIKE 'permtest-%';" 2>&1 | grep -qiE "permission denied" && echo "DENIED ✓" || echo "!!! allowed !!!"
echo -n "  TRUNCATE: "
docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c \
  "TRUNCATE mcp_calls;" 2>&1 | grep -qiE "permission denied" && echo "DENIED ✓" || echo "!!! allowed !!!"
echo -n "  grants: "
docker exec audit-pg psql -U "$PG_USER" -d "$PG_DB" -t -A -c \
  "SELECT string_agg(privilege_type,',' ORDER BY privilege_type) FROM information_schema.role_table_grants WHERE table_name='mcp_calls' AND grantee='policy_gateway_audit';" 2>&1 | grep -qiE "^INSERT$" && echo "INSERT-only ✓" || echo "(见上)"
