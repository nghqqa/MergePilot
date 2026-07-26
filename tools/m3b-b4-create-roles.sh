#!/bin/bash
# m3b-b4-create-roles.sh — 创建 B4 的两个 EXECUTE-only 账号 + 收敛授权。
#   policy_gateway_l2  : 仅 EXECUTE l2_claim/complete/fail/mark_unknown(Gateway L2 执行)
#   mergepilot_approver: 仅 EXECUTE l2_pending_list/l2_approve(approve CLI)
# 无任何表级 SELECT/INSERT/UPDATE。始终幂等收敛;--force 轮换密码。不打印密码前缀。
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-b4-create-roles.sh [--force]
set -euo pipefail

DIR=/home/ngh/.config/mergepilot
ENVF="$DIR/b4-roles.env"
mkdir -p "$DIR"; chmod 700 "$DIR"
FORCE=0; [ "${1:-}" = "--force" ] && FORCE=1

CTRL="$DIR/controller.env"
PG_SU=$(grep -E '^PG_USER=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep -E '^PG_DATABASE=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_DB=${PG_DB:-mergepilot_audit}

# 密码:env 存在且非 --force → 复用;否则生成
gen_pw(){ python3 -c "import secrets;print(secrets.token_urlsafe(24))"; }
if [ -f "$ENVF" ] && [ "$FORCE" = "0" ]; then
  L2_PW=$(grep '^POLICY_GATEWAY_L2_PASS=' "$ENVF" | head -1 | cut -d= -f2-)
  APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' "$ENVF" | head -1 | cut -d= -f2-)
  echo "复用现有 b4-roles.env 密码(--force 可轮换)"
else
  L2_PW=$(gen_pw); APV_PW=$(gen_pw)
  echo "生成新密码(--force 或首次)"
fi

# 始终收敛:建角色(若缺)+ 同步密码 + 收敛授权(无表级权限)
docker exec -i audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 <<SQL
DO \$do\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='policy_gateway_l2')   THEN CREATE ROLE policy_gateway_l2 LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mergepilot_approver') THEN CREATE ROLE mergepilot_approver LOGIN; END IF;
END \$do\$;
ALTER ROLE policy_gateway_l2   LOGIN PASSWORD '$L2_PW';
ALTER ROLE mergepilot_approver LOGIN PASSWORD '$APV_PW';
-- 收敛:撤全部表权限,只留函数 EXECUTE
REVOKE ALL ON approvals, policy_action_outbox, run_pr_bindings, mcp_calls, task_runs, stage_runs, stage_events, dispatch_outbox, controller_offsets FROM policy_gateway_l2, mergepilot_approver;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM policy_gateway_l2, mergepilot_approver;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM policy_gateway_l2, mergepilot_approver;
-- Gateway L2:仅 4 个函数
GRANT EXECUTE ON FUNCTION l2_claim_ticket(TEXT,TEXT,TEXT,INTEGER,TEXT)        TO policy_gateway_l2;
GRANT EXECUTE ON FUNCTION l2_complete_ticket(TEXT,UUID,TEXT)                  TO policy_gateway_l2;
GRANT EXECUTE ON FUNCTION l2_fail_ticket(TEXT,UUID,TEXT)                      TO policy_gateway_l2;
GRANT EXECUTE ON FUNCTION l2_mark_unknown(TEXT,UUID,TEXT)                     TO policy_gateway_l2;
-- Approver:仅 2 个函数
GRANT EXECUTE ON FUNCTION l2_pending_list()                                   TO mergepilot_approver;
GRANT EXECUTE ON FUNCTION l2_approve(TEXT,TEXT)                               TO mergepilot_approver;
SQL

cat > "$ENVF" <<EOF
POLICY_GATEWAY_L2_USER=policy_gateway_l2
POLICY_GATEWAY_L2_PASS=$L2_PW
MERGEPILOT_APPROVER_USER=mergepilot_approver
MERGEPILOT_APPROVER_PASS=$APV_PW
EOF
chmod 600 "$ENVF"
echo "roles converged (pass=<REDACTED>)"

# ─── 自检(用 B3.2 教训:if out=$(...) 处理预期失败)───
echo "=== 自检 ==="
SELFTEST_FAIL=0

# 表级访问必须被拒(逐项)
chk_deny(){ # $1=user $2=pw $3=label $4=sql
  local out rc
  if out=$(docker exec -e PGPASSWORD="$2" audit-pg psql -U "$1" -d "$PG_DB" -t -A -c "$4" 2>&1); then
    echo "  $3: !!! ALLOWED(应拒)"; SELFTEST_FAIL=1
  else
    rc=$?
    if echo "$out" | grep -qi "permission denied"; then echo "  $3: DENIED ✓ (rc=$rc)"
    else echo "  $3: UNEXPECTED(rc=$rc): $(echo "$out"|head -1)"; SELFTEST_FAIL=1; fi
  fi
}
for acct in "policy_gateway_l2:$L2_PW" "mergepilot_approver:$APV_PW"; do
  u="${acct%%:*}"; p="${acct#*:}"
  chk_deny "$u" "$p" "$u SELECT approvals" "SELECT count(*) FROM approvals;"
  chk_deny "$u" "$p" "$u INSERT outbox"   "INSERT INTO policy_action_outbox(ticket_id,run_id,action,repo,args_hash,idempotency_key) VALUES('x','x','merge','x','x','x');"
done

# 函数调用必须成功(用合法但无匹配的参数,验证 EXECUTE 权限)
fn_ok(){ # $1=user $2=pw $3=label $4=sql
  local out
  if out=$(docker exec -e PGPASSWORD="$2" audit-pg psql -U "$1" -d "$PG_DB" -t -A -c "$4" 2>&1); then
    echo "  $3: OK ✓ ($(echo "$out"|head -1|head -c 40))"
  else
    echo "  $3: FAIL(rc=$?): $(echo "$out"|head -1)"; SELFTEST_FAIL=1
  fi
}
fn_ok policy_gateway_l2 "$L2_PW" "gateway l2_claim_ticket(空匹配)" \
  "SELECT count(*) FROM l2_claim_ticket('nonexistent-tkt','merge','x',1,'nohash');"
fn_ok mergepilot_approver "$APV_PW" "approver l2_pending_list()" \
  "SELECT count(*) FROM l2_pending_list();"
fn_ok mergepilot_approver "$APV_PW" "approver l2_approve(不存在)" \
  "SELECT l2_approve('nonexistent-tkt','selftest@host');"

# 反向:gateway 不该能调 approver 函数,approver 不该能调 gateway 函数
chk_deny policy_gateway_l2 "$L2_PW" "gateway 越权 l2_approve" \
  "SELECT l2_approve('x','y');"
chk_deny mergepilot_approver "$APV_PW" "approver 越权 l2_claim_ticket" \
  "SELECT * FROM l2_claim_ticket('x','merge','x',1,'h');"

[ "$SELFTEST_FAIL" -ne 0 ] && { echo "!!! 自检失败,非零退出" >&2; exit 1; }
echo "self-test passed (B4 roles exit 0)"
