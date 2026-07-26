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

# ─── 2. UPDATE/DELETE/TRUNCATE 被权限拒绝 + 授权只有 INSERT ───
log ""; log "=== 2. UPDATE/DELETE/TRUNCATE 被拒 + grants=INSERT-only ==="
UPD=$(docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c "UPDATE mcp_calls SET decision='X' WHERE false;" 2>&1)
DEL=$(docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c "DELETE FROM mcp_calls WHERE false;" 2>&1)
TRC=$(docker exec -e PGPASSWORD="$PGW_PASS" audit-pg psql -U policy_gateway_audit -d "$PG_DB" -t -A -c "TRUNCATE mcp_calls;" 2>&1)
GRANTS=$(docker exec audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c \
  "SELECT string_agg(privilege_type,',' ORDER BY privilege_type) FROM information_schema.role_table_grants WHERE table_name='mcp_calls' AND grantee='policy_gateway_audit';" 2>/dev/null)
log "  UPDATE=$(echo $UPD|grep -oE 'permission denied'|head -1) DELETE=$(echo $DEL|grep -oE 'permission denied'|head -1) TRUNCATE=$(echo $TRC|grep -oE 'permission denied'|head -1) grants=$GRANTS"
echo "$UPD$DEL$TRC" | grep -qi "permission denied" && ok "UPDATE/DELETE/TRUNCATE 被权限拒绝" || bad "写权限未拒"
[ "$GRANTS" = "INSERT" ] && ok "授权仅 INSERT" || bad "授权异常: $GRANTS"

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

log ""
log "═══════════════════════════════════════════════"
log "  B3 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
if grep -rEo 'Bearer [A-Za-z0-9_-]{20,}' "$OUT" 2>/dev/null | head -1 | grep -q .; then
  echo "  !!! 输出含 Bearer 明文 !!!" >> "$OUT"
fi
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ] || exit 1
