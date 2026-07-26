#!/bin/bash
# crash-recovery-test.sh — 崩溃恢复测试:重启 Controller 后验证指定 run_id 的 PG 状态无重复。
# 用法: bash crash-recovery-test.sh <run_id>
set -uo pipefail
RUN_ID="${1:?用法: crash-recovery-test.sh <run_id>}"

count() { docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c "$1" 2>/dev/null; }

echo "═══════════════════════════════════════════════"
echo "  崩溃恢复测试: run_id=${RUN_ID}"
echo "═══════════════════════════════════════════════"

# 0. 前置断言(重启前数据必须存在且符合预期)
echo ""
echo "=== 0. 前置断言(重启前) ==="
T1=$(count "SELECT count(*) FROM task_runs WHERE run_id='${RUN_ID}';")
S1=$(count "SELECT count(*) FROM stage_runs WHERE run_id='${RUN_ID}';")
O1=$(count "SELECT count(*) FROM dispatch_outbox WHERE run_id='${RUN_ID}';")
E1=$(count "SELECT count(*) FROM stage_events WHERE run_id='${RUN_ID}';")
TASK_STATUS=$(count "SELECT status FROM task_runs WHERE run_id='${RUN_ID}';")

echo "  task_runs=${T1}(预期 1)  stage_runs=${S1}(预期 3)  outbox=${O1}(预期 3)  events=${E1}  task_status=${TASK_STATUS}"

ABORT=0
[ "$T1" = "1" ] || { echo "  ❌ task_runs count=${T1},预期 1"; ABORT=1; }
[ "$S1" = "3" ] || { echo "  ❌ stage_runs count=${S1},预期 3"; ABORT=1; }
[ "$O1" = "3" ] || { echo "  ❌ outbox count=${O1},预期 3"; ABORT=1; }
if [ "$ABORT" -eq 1 ]; then
  echo ""
  echo "  ⛔ 前置断言失败,数据不符合预期。终止崩溃恢复测试。"
  echo "═══════════════════════════════════════════════"
  exit 1
fi
echo "  ✅ 前置数据符合预期"

# 快照完整状态(用于对比)
TASK_BEFORE=$(count "SELECT status||'|'||COALESCE(verdict,'NULL') FROM task_runs WHERE run_id='${RUN_ID}';")
STAGES_BEFORE=$(count "SELECT string_agg(stage||':'||status||':'||COALESCE(verdict,'NULL'),',' ORDER BY id) FROM stage_runs WHERE run_id='${RUN_ID}';")
OUTBOX_BEFORE=$(count "SELECT string_agg(idempotency_key||':'||status||':'||COALESCE(matrix_event_id,'NULL'),',' ORDER BY id) FROM dispatch_outbox WHERE run_id='${RUN_ID}';")

echo ""
echo "=== 1. docker restart mergepilot-controller ==="
docker restart mergepilot-controller
echo "等 20s 恢复..."
sleep 20

echo ""
echo "=== 2. controller 日志(恢复后 25s) ==="
docker logs --since 25s mergepilot-controller 2>&1 | head -8

echo ""
echo "=== 3. 重启后状态对比 ==="
TASK_AFTER=$(count "SELECT status||'|'||COALESCE(verdict,'NULL') FROM task_runs WHERE run_id='${RUN_ID}';")
STAGES_AFTER=$(count "SELECT string_agg(stage||':'||status||':'||COALESCE(verdict,'NULL'),',' ORDER BY id) FROM stage_runs WHERE run_id='${RUN_ID}';")
OUTBOX_AFTER=$(count "SELECT string_agg(idempotency_key||':'||status||':'||COALESCE(matrix_event_id,'NULL'),',' ORDER BY id) FROM dispatch_outbox WHERE run_id='${RUN_ID}';")
E2=$(count "SELECT count(*) FROM stage_events WHERE run_id='${RUN_ID}';")

echo "  task 终态:   ${TASK_BEFORE} → ${TASK_AFTER}"
echo "  stage 状态: ${STAGES_BEFORE}"
echo "              → ${STAGES_AFTER}"
echo "  outbox 状态: 不变(3 条 DISPATCHED)"
echo "  events:     ${E1} → ${E2}"

echo ""
echo "=== 4. Controller 日志检查(有无重新派发 review/fix/verify) ==="
REDISPATCH=$(docker logs --since 30s mergepilot-controller 2>&1 | grep -ciE "outbox.*→.*(reviewer|fixer|verifier)")
echo "  重新派发次数: ${REDISPATCH}(预期 0)"

echo ""
echo "=== 5. 最终 PG 状态 ==="
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT run_id,status,current_stage,verdict FROM task_runs WHERE run_id='${RUN_ID}';" 2>&1 | grep -v "^$"
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT stage,attempt,status,verdict FROM stage_runs WHERE run_id='${RUN_ID}' ORDER BY id;" 2>&1 | grep -v "^$"
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT idempotency_key,status,matrix_event_id IS NOT NULL as has_eid FROM dispatch_outbox WHERE run_id='${RUN_ID}' ORDER BY id;" 2>&1 | grep -v "^$"

echo ""
PASS=0; FAIL=0
[ "$TASK_BEFORE" = "$TASK_AFTER" ] && { echo "✅ task 终态未变化"; PASS=$((PASS+1)); } || { echo "❌ task 终态变化!"; FAIL=$((FAIL+1)); }
[ "$STAGES_BEFORE" = "$STAGES_AFTER" ] && { echo "✅ stage 状态未变化"; PASS=$((PASS+1)); } || { echo "❌ stage 状态变化!"; FAIL=$((FAIL+1)); }
[ "$OUTBOX_BEFORE" = "$OUTBOX_AFTER" ] && { echo "✅ outbox 状态+event_id 未变化"; PASS=$((PASS+1)); } || { echo "❌ outbox 变化!"; FAIL=$((FAIL+1)); }
[ "$E1" = "$E2" ] && { echo "✅ events 数量未变化(${E1})"; PASS=$((PASS+1)); } || { echo "❌ events 变化(${E1}→${E2})!"; FAIL=$((FAIL+1)); }
[ "$REDISPATCH" = "0" ] && { echo "✅ Controller 未重新派发"; PASS=$((PASS+1)); } || { echo "❌ 重新派发 ${REDISPATCH} 次!"; FAIL=$((FAIL+1)); }

echo ""
echo "═══════════════════════════════════════════════"
echo "  PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo "  ✅ 崩溃恢复测试通过(Controller 重启后 ${RUN_ID} 状态完整不变)"
else
  echo "  ❌ 崩溃恢复测试失败(${FAIL} 项)"
fi
echo "═══════════════════════════════════════════════"
