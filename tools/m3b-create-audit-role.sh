#!/bin/bash
# m3b-create-audit-role.sh — 创建/收敛 Gateway 专用 INSERT-only 审计账号。
# B3.1:始终幂等执行角色 + GRANT/REVOKE 收敛(不因 audit-db.env 存在而跳过);
#       --force 仅负责轮换密码。ON_ERROR_STOP=1。不打印密码前缀。
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-create-audit-role.sh [--force]
set -euo pipefail

DIR=/home/ngh/.config/mergepilot
ENVF="$DIR/audit-db.env"
mkdir -p "$DIR"; chmod 700 "$DIR"
FORCE=0; [ "${1:-}" = "--force" ] && FORCE=1

CTRL="$DIR/controller.env"
PG_SU=$(grep -E '^PG_USER=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep -E '^PG_DATABASE=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_DB=${PG_DB:-mergepilot_audit}

# ─── 决定密码:env 存在且非 --force → 复用;否则生成 + 写 env ───
if [ -f "$ENVF" ] && [ "$FORCE" = "0" ]; then
  PW=$(grep '^PGW_AUDIT_PASS=' "$ENVF" | head -1 | cut -d= -f2-)
  echo "复用现有 audit-db.env 密码(--force 可轮换)"
else
  PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
  echo "生成新密码(--force 或首次)"
fi

# ─── 始终收敛:建角色(若缺)→ 同步密码 → GRANT/REVOKE ───
# 非引用 heredoc:shell 展开 $PW(token_urlsafe 安全字符集,单引号字面量)。
# DO block 内不用 $PW(psql var 在 dollar-quoted body 内不插值);密码在顶层 ALTER 同步。
docker exec -i audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 <<SQL
DO \$do\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_audit') THEN
    CREATE ROLE policy_gateway_audit LOGIN;
  END IF;
END \$do\$;
ALTER ROLE policy_gateway_audit LOGIN PASSWORD '$PW';
GRANT CONNECT ON DATABASE mergepilot_audit TO policy_gateway_audit;
GRANT USAGE ON SCHEMA public TO policy_gateway_audit;
-- mcp_calls:只 INSERT
REVOKE ALL ON mcp_calls FROM policy_gateway_audit;
GRANT INSERT ON mcp_calls TO policy_gateway_audit;
REVOKE SELECT, UPDATE, DELETE, TRUNCATE ON mcp_calls FROM policy_gateway_audit;
-- 审计账号不能碰票据/outbox(B4 用单独账号)
REVOKE ALL ON approvals, policy_action_outbox FROM policy_gateway_audit;
-- B3.1 非阻断加固:收窄 PUBLIC 在数据库上的临时表权限
REVOKE CREATE, TEMP ON DATABASE mergepilot_audit FROM PUBLIC;
SQL

# 写 env(密码可能新生成也可能复用,都写一遍保证一致)
cat > "$ENVF" <<EOF
PGW_AUDIT_USER=policy_gateway_audit
PGW_AUDIT_PASS=$PW
PGW_AUDIT_DB=$PG_DB
EOF
chmod 600 "$ENVF"
echo "role policy_gateway_audit converged to INSERT-only on mcp_calls (pass=<REDACTED> len=${#PW})"

# ─── 权限自检(B3.2:用 if out=$(...) 显式处理 psql 退出码,避免 set -e 在预期失败处中止)───
# 关键:被拒的 SELECT/UPDATE/... 让 psql 返回 1;`out=$(psql ...)` 是 bare 赋值,
# set -e 会在此中止脚本 → 后续判断不执行 → 角色脚本非零退出 → 部署链 abort。
# 解法:放进 `if out=$(...)` 条件里(set -e 对条件中的命令不触发),再按退出码分支。
echo "=== 权限自检 ==="
SELFTEST_FAIL=0

# INSERT:期望 psql 成功(exit 0)+ "INSERT 0"
echo -n "  INSERT: "
if ins=$(docker exec -e PGPASSWORD="$PW" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c \
   "INSERT INTO mcp_calls(request_id,caller_agent,tool,decision,phase) VALUES('permtest-'||md5(random()::text),'selftest','(permtest)','ALLOW','INTENT');" 2>&1); then
  echo "$ins" | grep -qi "INSERT 0" && echo "OK ✓" || { echo "FAIL(无 INSERT 0): $ins"; SELFTEST_FAIL=1; }
else
  echo "FAIL(rc=$?): $ins"; SELFTEST_FAIL=1
fi

# 期望被拒的操作:psql 应失败,且 stderr 含 permission denied
chk_deny(){
  local label="$1" sql="$2" out rc
  echo -n "  $label: "
  if out=$(docker exec -e PGPASSWORD="$PW" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c "$sql" 2>&1); then
    echo "!!! ALLOWED(应被拒)"; SELFTEST_FAIL=1        # psql 成功 = 权限给了 = 错
  else
    rc=$?
    if echo "$out" | grep -qi "permission denied"; then echo "DENIED ✓ (rc=$rc)"
    else echo "UNEXPECTED(rc=$rc): $(echo "$out" | head -1)"; SELFTEST_FAIL=1; fi
  fi
}
chk_deny "SELECT"   "SELECT count(*) FROM mcp_calls;"
chk_deny "UPDATE"   "UPDATE mcp_calls SET decision='X' WHERE false;"
chk_deny "DELETE"   "DELETE FROM mcp_calls WHERE false;"
chk_deny "TRUNCATE" "TRUNCATE mcp_calls;"

# grants:必须恰好 = INSERT
echo -n "  grants: "
GR=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT coalesce(string_agg(privilege_type,',' ORDER BY privilege_type),'(none)') FROM information_schema.role_table_grants WHERE table_name='mcp_calls' AND grantee='policy_gateway_audit';" 2>&1) || { GR="(query failed)"; SELFTEST_FAIL=1; }
echo "$GR"
[ "$GR" = "INSERT" ] || { echo "  !!! grants 异常(应 INSERT)"; SELFTEST_FAIL=1; }

# B3.2:任何自检失败 → 非零退出(让 run-policy-gateway.sh 的 set -e 捕获,中止部署)
if [ "$SELFTEST_FAIL" -ne 0 ]; then
  echo "!!! 自检失败($SELFTEST_FAIL 项),角色脚本非零退出" >&2
  exit 1
fi
echo "self-test passed (role script exits 0)"
