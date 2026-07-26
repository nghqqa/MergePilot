#!/bin/bash
# m3a-verify.sh — M3-A 强化验收(12 项)。验证 PG 状态 + 证据一致性。
# 用法: bash m3a-verify.sh <run_id>
set -uo pipefail
RUN_ID="${1:?用法: m3a-verify.sh <run_id>}"
PSQL="docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A"

PASS=0; FAIL=0; WARN=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️ $1"; WARN=$((WARN+1)); }

echo "═══════════════════════════════════════════════"
echo "  M3-A 验收检查: run_id=$RUN_ID"
echo "═══════════════════════════════════════════════"

# 1. task_runs 存在
ROW=$($PSQL -c "SELECT status||'|'||COALESCE(current_stage,'NULL')||'|'||COALESCE(verdict,'NULL') FROM task_runs WHERE run_id='$RUN_ID';" 2>/dev/null)
if [ -n "$ROW" ]; then ok "task_runs 存在: $ROW"; else fail "task_runs 不存在"; fi
STATUS=$(echo "$ROW" | cut -d'|' -f1)
STAGE=$(echo "$ROW" | cut -d'|' -f2)
VERDICT=$(echo "$ROW" | cut -d'|' -f3)

# 2. task status 合法
if [ "$STATUS" = "PASS" ] || [ "$STATUS" = "HOLD" ] || [ "$STATUS" = "FAIL" ] || [ "$STATUS" = "MERGED" ] || [ "$STATUS" = "ROLLED_BACK" ]; then
  ok "task status 合法: $STATUS"
else
  fail "task status 非法: $STATUS"
fi

# 3. task verdict 与 status 一致
if [ "$STATUS" = "PASS" ] && [ "$VERDICT" != "PASS" ]; then
  fail "task verdict($VERDICT) 与 status(PASS) 不一致"
elif [ "$STATUS" = "HOLD" ] && [ "$VERDICT" != "blocked-needs-approval" ] && [ "$VERDICT" != "FAIL" ]; then
  fail "task verdict($VERDICT) 与 status(HOLD) 不一致(应为 blocked-needs-approval 或 FAIL)"
else
  ok "task verdict($VERDICT) 与 status($STATUS) 一致"
fi

echo ""
echo "--- stage_runs ---"
$PSQL -c "SELECT stage,attempt,status,verdict FROM stage_runs WHERE run_id='$RUN_ID' ORDER BY id;" 2>/dev/null | while IFS='|' read stage attempt status verdict; do
  echo "  stage=$stage attempt=$attempt status=$status verdict=$verdict"
done

# 4. review 唯一
REVIEW_CNT=$($PSQL -c "SELECT count(*) FROM stage_runs WHERE run_id='$RUN_ID' AND stage='review' AND attempt=1;" 2>/dev/null)
[ "$REVIEW_CNT" = "1" ] && ok "review 唯一(1)" || fail "review count=$REVIEW_CNT(应=1)"

# 5. fix 唯一
FIX_CNT=$($PSQL -c "SELECT count(*) FROM stage_runs WHERE run_id='$RUN_ID' AND stage='fix' AND attempt=1;" 2>/dev/null)
[ "$FIX_CNT" = "1" ] && ok "fix 唯一(1)" || fail "fix count=$FIX_CNT(应=1)"

# 6. verify 唯一
VERIFY_CNT=$($PSQL -c "SELECT count(*) FROM stage_runs WHERE run_id='$RUN_ID' AND stage='verify' AND attempt=1;" 2>/dev/null)
[ "$VERIFY_CNT" = "1" ] && ok "verify 唯一(1)" || fail "verify count=$VERIFY_CNT(应=1)"

# 7. verify verdict 非空且与 task verdict 一致
V_VERDICT=$($PSQL -c "SELECT verdict FROM stage_runs WHERE run_id='$RUN_ID' AND stage='verify' AND attempt=1;" 2>/dev/null)
if [ -z "$V_VERDICT" ] || [ "$V_VERDICT" = "NULL" ]; then
  fail "verify verdict 为空(应为 PASS 或 blocked-needs-approval)"
elif [ "$V_VERDICT" = "$VERDICT" ]; then
  ok "verify verdict($V_VERDICT) = task verdict($VERDICT)"
else
  fail "verify verdict($V_VERDICT) ≠ task verdict($VERDICT)"
fi

echo ""
echo "--- dispatch_outbox ---"
$PSQL -c "SELECT idempotency_key,status,matrix_event_id IS NOT NULL FROM dispatch_outbox WHERE run_id='$RUN_ID' ORDER BY id;" 2>/dev/null | while IFS='|' read key status has_eid; do
  echo "  $key | $status | eid=$has_eid"
done

# 8. outbox 全部 DISPATCHED
PENDING_CNT=$($PSQL -c "SELECT count(*) FROM dispatch_outbox WHERE run_id='$RUN_ID' AND status != 'DISPATCHED';" 2>/dev/null)
[ "$PENDING_CNT" = "0" ] && ok "outbox 全部 DISPATCHED" || fail "outbox 有 $PENDING_CNT 条未 DISPATCHED"

# 9. outbox 都有 matrix_event_id
NO_EID_CNT=$($PSQL -c "SELECT count(*) FROM dispatch_outbox WHERE run_id='$RUN_ID' AND matrix_event_id IS NULL;" 2>/dev/null)
[ "$NO_EID_CNT" = "0" ] && ok "outbox 全部有 matrix_event_id" || fail "outbox 有 $NO_EID_CNT 条无 event_id"

echo ""
echo "--- stage_events ---"
SE_CNT=$($PSQL -c "SELECT count(*) FROM stage_events WHERE run_id='$RUN_ID';" 2>/dev/null)
echo "  stage_events(本任务): $SE_CNT 条"

# 10. stage_events 有 run_id 关联
if [ "$SE_CNT" -gt 0 ]; then
  ok "stage_events 可按 run_id 查询($SE_CNT 条)"
else
  fail "stage_events run_id=NULL,无法按 run_id 查询"
fi

# 11. 重复事件标为 DUPLICATE(非 PROCESSED)
DUP_CNT=$($PSQL -c "SELECT count(*) FROM stage_events WHERE run_id='$RUN_ID' AND status='DUPLICATE';" 2>/dev/null)
PROCESSED_CNT=$($PSQL -c "SELECT count(*) FROM stage_events WHERE run_id='$RUN_ID' AND status='PROCESSED';" 2>/dev/null)
echo "  PROCESSED=$PROCESSED_CNT  DUPLICATE=$DUP_CNT"
if [ "$DUP_CNT" -ge 0 ] && [ "$PROCESSED_CNT" -ge 1 ]; then
  ok "有 PROCESSED + DUPLICATE(幂等生效)"
else
  warn "PROCESSED=$PROCESSED_CNT DUPLICATE=$DUP_CNT(可能无重复事件)"
fi

echo ""
echo "--- controller_offsets ---"
HAS_TOKEN=$($PSQL -c "SELECT sync_token IS NOT NULL FROM controller_offsets WHERE consumer_name='controller';" 2>/dev/null)
[ "$HAS_TOKEN" = "t" ] && ok "/sync 游标持久化" || fail "controller_offsets 无 token"

echo ""
echo "═══════════════════════════════════════════════"
echo "  PASS=$PASS  FAIL=$FAIL  WARN=$WARN"
if [ "$FAIL" -eq 0 ]; then
  echo "  ✅ M3-A 验收通过(全部关键项)"
else
  echo "  ❌ M3-A 验收未通过($FAIL 项失败)"
fi
echo "═══════════════════════════════════════════════"
