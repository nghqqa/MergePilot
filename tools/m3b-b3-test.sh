#!/bin/bash
# m3b-b3-test.sh — B3 审计权限 + 写 fail-closed 验收(10 项)。
# 用独立坏-DSN gateway(policy-gw-noaudit)测审计不可用,不破坏生产审计库。
# 退出码:全过 0,否则 1。
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b3-test.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

CTRL=/home/ngh/.config/mergepilot/controller.env
AUDITF=/home/ngh/.config/mergepilot/audit-db.env
TOKENS=/home/ngh/.config/mergepilot/role-tokens.json
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_DB=${PG_DB:-mergepilot_audit}
PGW_PASS=$(grep '^PGW_AUDIT_PASS=' "$AUDITF" | head -1 | cut -d= -f2-)
ROLE_TOKENS=$(python3 -c "import json;print(json.dumps(json.load(open('$TOKENS'))))")
START_TS=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "SELECT now();" 2>/dev/null)

log "═══════════════════════════════════════════════"
log "  B3 验收(独立 INSERT-only 账号 + 写 fail-closed)"
log "═══════════════════════════════════════════════"

# ─── 1. INSERT-only 账号:INSERT 通 ───
log ""; log "=== 1. policy_gateway_audit INSERT 成功 ==="
INS=$(docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c \
  "INSERT INTO mcp_calls(request_id,correlation_id,phase,caller_agent,tool,decision,reason_code) VALUES('b3test-'||md5(random()::text),'b3selftest','INTENT','selftest','(b3permtest)','ALLOW','SELFTEST');" 2>&1)
echo "$INS" | grep -qi "INSERT 0" && ok "audit 账号 INSERT 成功" || bad "INSERT 失败: $INS"

# ─── 2. UPDATE/DELETE/TRUNCATE 逐项被拒 + 授权只有 INSERT(B3.1:不拼接)───
log ""; log "=== 2. UPDATE/DELETE/TRUNCATE 逐项被拒 + grants=INSERT-only ==="
chk_priv(){ # $1=label $2=sql → 期望 permission denied
  local out; out=$(docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c "$2" 2>&1)
  echo "$out" | grep -qi "permission denied" && { ok "$1 → permission denied"; } || { bad "$1 未拒: $(echo "$out"|head -1)"; }
}
chk_priv "SELECT"  "SELECT count(*) FROM mcp_calls;"
chk_priv "UPDATE"  "UPDATE mcp_calls SET decision='X' WHERE false;"
chk_priv "DELETE"  "DELETE FROM mcp_calls WHERE false;"
chk_priv "TRUNCATE" "TRUNCATE mcp_calls;"
GRANTS=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT coalesce(string_agg(privilege_type,',' ORDER BY privilege_type),'(none)') FROM information_schema.role_table_grants WHERE table_name='mcp_calls' AND grantee='policy_gateway_audit';" 2>/dev/null)
log "  grants=$GRANTS"
[ "$GRANTS" = "INSERT" ] && ok "授权仅 INSERT" || bad "授权异常: $GRANTS"

# ─── 2b. phase CHECK 约束存在 + 非法 phase 被拒(B3.1)───
log ""; log "=== 2b. phase CHECK 约束:非法 phase 插入被拒 ==="
PCHECK=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT count(*) FROM pg_constraint WHERE conname='mcp_calls_phase_check' AND conrelid='mcp_calls'::regclass;" 2>/dev/null)
log "  mcp_calls_phase_check 存在: $PCHECK"
[ "${PCHECK:-0}" = "1" ] && ok "phase CHECK 约束存在" || bad "phase CHECK 缺失"
BADPH=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "INSERT INTO mcp_calls(request_id,caller_agent,tool,decision,phase) VALUES('b3bad-'||md5(random()::text),'selftest','(b3phase)','ALLOW','BOGUS');" 2>&1)
echo "$BADPH" | grep -qiE "violates check|mcp_calls_phase_check" && ok "非法 phase=BOGUS 被 CHECK 拒" || bad "非法 phase 未拒: $(echo "$BADPH"|head -1)"

# ─── 2c. 恢复场景:drift → 角色脚本(无 --force)收敛 + 必须 exit 0(B3.2)───
log ""; log "=== 2c. drift(GRANT SELECT)→ 角色脚本收敛回 INSERT-only + exit 0 ==="
docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -c "GRANT SELECT ON mcp_calls TO policy_gateway_audit;" >/dev/null 2>&1
DRIFT=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT coalesce(string_agg(privilege_type,',' ORDER BY privilege_type),'(none)') FROM information_schema.role_table_grants WHERE table_name='mcp_calls' AND grantee='policy_gateway_audit';" 2>/dev/null)
log "  drift 后 grants=$DRIFT(应 = INSERT,SELECT)"
[ "$DRIFT" = "INSERT,SELECT" ] && ok "drift 生效(grants=INSERT,SELECT)" || bad "drift 未生效: $DRIFT"
# 收敛:子 shell + trap 保证中断也恢复 SELECT(gateway 只 INSERT,SELECT 漂移不影响业务但仍收敛)
CONV_RC=0
( trap 'docker exec audit-pg psql -U "'"$PG_SU"'" -d "'"$PG_DB"'" -c "REVOKE SELECT ON mcp_calls FROM policy_gateway_audit;" >/dev/null 2>&1' EXIT
  bash /mnt/d/goai/tools/m3b-create-audit-role.sh ) > /tmp/b3_drift_conv.out 2>&1 || CONV_RC=$?
log "  角色脚本 exit=$CONV_RC"
tail -3 /tmp/b3_drift_conv.out >> "$OUT"
[ "$CONV_RC" -eq 0 ] && ok "角色脚本收敛后 exit 0(B3.2 回归修复)" || bad "角色脚本 exit=$CONV_RC(set -e 回归?)"
CONV=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT coalesce(string_agg(privilege_type,',' ORDER BY privilege_type),'(none)') FROM information_schema.role_table_grants WHERE table_name='mcp_calls' AND grantee='policy_gateway_audit';" 2>/dev/null)
log "  收敛后 grants=$CONV"
[ "$CONV" = "INSERT" ] && ok "drift 后自动收敛回 INSERT-only" || bad "未收敛: $CONV"

# ─── 3 & 7. 写产生 INTENT,与 RESULT 共享 correlation_id ───
log ""; log "=== 3+7. 写产生 INTENT 先于 GitHub;INTENT/RESULT 同 correlation_id ==="
UNIQ="fix/b3-corr-$$"
CB=$(docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_branch owner=nghqqa repo=MergePilot branch="$UNIQ" from_branch=main 2>&1 | head -1)
echo "$CB" | grep -qiE "ref.*fix/b3-corr" && ok "create_branch 执行成功" || bad "create_branch 失败: $(echo "$CB"|head -c 80)"
CORR=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT correlation_id FROM mcp_calls WHERE caller_agent='fixer' AND tool='create_branch' AND phase='INTENT' AND ts>'$START_TS' ORDER BY ts DESC LIMIT 1;" 2>/dev/null)
PHASES=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT string_agg(phase,',' ORDER BY phase) FROM mcp_calls WHERE correlation_id='$CORR';" 2>/dev/null)
log "  corr=$CORR phases=[$PHASES]"
[ -n "$CORR" ] && echo "$PHASES" | grep -q "INTENT" && echo "$PHASES" | grep -q "RESULT" && ok "INTENT+RESULT 共享 correlation_id" || bad "correlation 不完整(corr=$CORR phases=$PHASES)"

# ─── 8. 审计不存原始内容/token(只 args_hash)───
log ""; log "=== 8. 审计不含原始入参/token(只 args_hash)==="
HAS_RAW=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE args_hash IS NOT NULL AND (error LIKE '%Bearer%' OR target_repo LIKE '%password%');" 2>/dev/null)
HASHFMT=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE args_hash IS NOT NULL AND args_hash != '' AND args_hash !~ '^[a-f0-9]{16}$';" 2>/dev/null)
log "  含疑似敏感:$HAS_RAW  args_hash 非法格式:$HASHFMT  (mcp_calls 无 content/args 列,只 args_hash)"
[ "${HAS_RAW:-0}" = "0" ] && [ "${HASHFMT:-0}" = "0" ] && ok "审计无原始内容/token,args_hash 为 16 位 hex" || bad "审计疑似含敏感数据"

# ─── 9. L2 仍 L2_TICKET_REQUIRED(B3 不提前放行)───
log ""; log "=== 9. coordinator merge 仍 → L2_TICKET_REQUIRED ==="
docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py 2>/dev/null
L2=$(docker exec policy-gw python3 /tmp/probe-tools.py coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=999 2>&1 | head -3)
echo "$L2" | tail -1 >> "$OUT"
echo "$L2" | grep -qiE "POLICY_DENIED.*L2_TICKET_REQUIRED" && ok "L2 仍需票据(B3 不放行)" || bad "L2 异常"

# ─── 4 & 5 & 6. 独立坏-DSN gateway:写 AUDIT_UNAVAILABLE + 无 GitHub 副作用 + 读仍可 ===
log ""; log "=== 4+5+6. 坏-DSN gateway(audit 不可用)==="
docker rm -f policy-gw-noaudit 2>/dev/null
docker run -d --name policy-gw-noaudit --network hiclab-net --restart no \
  -e ROLE_TOKENS="$ROLE_TOKENS" \
  -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="postgresql://policy_gateway_audit:wrong@audit-pg-unreachable:5432/mergepilot_audit" \
  policy-gateway:latest >/dev/null
docker network connect mcp-backend-net policy-gw-noaudit 2>/dev/null
# 等它起 + 连上游
for i in $(seq 1 15); do
  docker logs policy-gw-noaudit 2>&1 | grep -qa "upstream ready" && break
  sleep 1
done
docker logs --tail 2 policy-gw-noaudit 2>&1 | grep -a "upstream ready" >> "$OUT" || docker logs --tail 5 policy-gw-noaudit 2>&1 >> "$OUT"

log ""
log "=== 6. 坏-DSN 下,只读调用仍可执行(get_me)==="
docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw-noaudit:/tmp/probe-tools.py 2>/dev/null
RD=$(docker exec policy-gw-noaudit python3 /tmp/probe-tools.py reviewer --call get_me owner=nghqqa repo=MergePilot 2>&1 | head -1)
echo "$RD" | head -c 80 >> "$OUT"
echo "$RD" | grep -qiE "login|nghqqa" && ok "audit 不可用时,只读仍可执行" || bad "只读失败: $(echo "$RD"|head -c 80)"

log ""
log "=== 4+5. 坏-DSN 下,写调用 → AUDIT_UNAVAILABLE 且无 GitHub 副作用 ==="
NOAUDIT_BRANCH="fix/b3-no-side-effect-$$"
WR=$(docker exec policy-gw-noaudit python3 /tmp/probe-tools.py fixer --call create_branch owner=nghqqa repo=MergePilot branch="$NOAUDIT_BRANCH" from_branch=main 2>&1 | head -3)
echo "$WR" | tail -1 >> "$OUT"
echo "$WR" | grep -qiE "POLICY_DENIED.*AUDIT_UNAVAILABLE" && ok "写返回 AUDIT_UNAVAILABLE(fail-closed)" || bad "写应 AUDIT_UNAVAILABLE: $(echo "$WR"|tail -1)"
# 网关日志不应有 forward
FWD=$(docker logs policy-gw-noaudit 2>&1 | grep -acE "ALLOW.*→ forward.*create_branch")
log "  坏-DSN gateway 日志中 create_branch forward 次数: $FWD(应 0)"
[ "${FWD:-0}" = "0" ] && ok "审计不可用时未转发 GitHub(无副作用)" || bad "审计不可用时仍转发 GitHub!"
# 直接验 GitHub 上无该分支
BREX=$(docker exec policy-gw python3 /tmp/probe-tools.py reviewer --call get_file_contents owner=nghqqa repo=MergePilot path=README.md 2>&1 | head -1)
# 用 list_branches 验证分支不存在(更直接)
BRCHK=$(docker exec policy-gw python3 /tmp/probe-tools.py reviewer --call list_branches owner=nghqqa repo=MergePilot 2>&1 | grep -c "$NOAUDIT_BRANCH")
log "  GitHub 上 $NOAUDIT_BRANCH 出现次数: $BRCHK(应 0)"
[ "${BRCHK:-0}" = "0" ] && ok "GitHub 上无该分支(确认无副作用)" || bad "GitHub 上有该分支(有副作用!)"

# 清理坏-DSN gateway
docker stop policy-gw-noaudit >/dev/null 2>&1 && docker rm policy-gw-noaudit >/dev/null 2>&1

# ─── fail-fast: 坏 schema 变体 → 脚本非零退出 + 不替换现有 gateway(B3.1)───
log ""; log "=== fail-fast: schema 失败 → 非零退出 + gateway 容器不替换 ==="
GW_BEFORE=$(docker inspect policy-gw --format '{{.Id}}' 2>/dev/null | cut -c1-12)
echo "BROKEN SYNTAX ;;; NOT SQL ;;" > /tmp/b3_broken_schema.sql
sed 's#/mnt/d/goai/tools/audit-db/m3b_policy.sql#/tmp/b3_broken_schema.sql#g' \
  /mnt/d/goai/tools/run-policy-gateway.sh > /tmp/b3_run_broken.sh
bash /tmp/b3_run_broken.sh >/tmp/b3_ff.out 2>&1; FF_RC=$?
GW_AFTER=$(docker inspect policy-gw --format '{{.Id}}' 2>/dev/null | cut -c1-12)
rm -f /tmp/b3_broken_schema.sql /tmp/b3_run_broken.sh
log "  exit=$FF_RC  gw_before=$GW_BEFORE  gw_after=$GW_AFTER"
grep -iE "schema 初始化失败|中止" /tmp/b3_ff.out 2>/dev/null | head -1 | sed 's/^/    [broken-script output] /' >> "$OUT"
[ "$FF_RC" -ne 0 ] && ok "schema 失败 → 脚本非零退出($FF_RC)" || bad "应非零退出,实际 $FF_RC"
[ "$GW_BEFORE" = "$GW_AFTER" ] && ok "失败后 gateway 容器未被替换(fail-fast)" || bad "失败后 gateway 被替换了!"

# ─── 正常部署:完整 run-policy-gateway.sh → 容器替换 + healthy(B3.2:确认角色脚本不再 abort 部署)───
log ""; log "=== 正常部署:run-policy-gateway.sh 完整跑 → 容器替换 + healthy ==="
GW_OLD=$(docker inspect policy-gw --format '{{.Id}}' 2>/dev/null | cut -c1-12)
bash /mnt/d/goai/tools/run-policy-gateway.sh > /tmp/b3_deploy.out 2>&1; DEPLOY_RC=$?
sleep 8
GW_NEW=$(docker inspect policy-gw --format '{{.Id}}' 2>/dev/null | cut -c1-12)
HEALTH=$(docker inspect policy-gw --format '{{.State.Health.Status}}' 2>/dev/null)
log "  deploy_rc=$DEPLOY_RC  gw_old=$GW_OLD  gw_new=$GW_NEW  health=$HEALTH"
[ "$DEPLOY_RC" -eq 0 ] && ok "run-policy-gateway.sh 完整跑 exit 0(角色脚本不再 abort 部署)" || bad "deploy exit=$DEPLOY_RC(可能 set -e 回归)"
[ "$GW_OLD" != "$GW_NEW" ] && ok "gateway 容器已替换(正常部署)" || bad "容器未替换"
[ "$HEALTH" = "healthy" ] && ok "gateway 恢复 healthy" || bad "health=$HEALTH"
# 重新装载探针(新容器 /tmp 为空)
docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py >/dev/null 2>&1

log ""
log "═══════════════════════════════════════════════"
log "  B3 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
if grep -rEo 'Bearer [A-Za-z0-9_-]{20,}' "$OUT" 2>/dev/null | head -1 | grep -q .; then
  echo "  ❌ 输出含 Bearer 明文(凭证扫描失败)" >> "$OUT"
  FAIL=$((FAIL+1))
fi
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ] || exit 1
