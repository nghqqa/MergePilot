#!/bin/bash
# crash-recovery-test.sh — 崩溃恢复测试:重启 Controller 后验证 PG 状态无重复。
set -uo pipefail
RUN_ID="m3a-e2e-20260726-01"

count() { docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c "$1" 2>/dev/null; }

echo "═══════════════════════════════════════════════"
echo "  崩溃恢复测试: docker restart mergepilot-controller"
echo "═══════════════════════════════════════════════"

echo ""
echo "=== 1. 重启前计数 ==="
T1=$(count "SELECT count(*) FROM task_runs;")
S1=$(count "SELECT count(*) FROM stage_runs WHERE run_id='${RUN_ID}';")
O1=$(count "SELECT count(*) FROM dispatch_outbox WHERE run_id='${RUN_ID}';")
E1=$(count "SELECT count(*) FROM stage_events;")
echo "task_runs=$T1  stage_runs=$S1  outbox=$O1  events=$E1"

echo ""
echo "=== 2. docker restart ==="
docker restart mergepilot-controller
echo "等 20s 恢复..."
sleep 20

echo ""
echo "=== 3. controller 日志(恢复后 25s) ==="
docker logs --since 25s mergepilot-controller 2>&1 | head -8

echo ""
echo "=== 4. 重启后计数 + 对比 ==="
T2=$(count "SELECT count(*) FROM task_runs;")
S2=$(count "SELECT count(*) FROM stage_runs WHERE run_id='${RUN_ID}';")
O2=$(count "SELECT count(*) FROM dispatch_outbox WHERE run_id='${RUN_ID}';")
E2=$(count "SELECT count(*) FROM stage_events;")

echo "task_runs:    $T1 → $T2"
echo "stage_runs:   $S1 → $S2"
echo "outbox:       $O1 → $O2"
echo "stage_events: $E1 → $E2"

echo ""
PASS=0; FAIL=0
[ "$T1" = "$T2" ] && { echo "✅ task_runs 无变化"; PASS=$((PASS+1)); } || { echo "❌ task_runs 变化!"; FAIL=$((FAIL+1)); }
[ "$S1" = "$S2" ] && { echo "✅ stage_runs 无变化"; PASS=$((PASS+1)); } || { echo "❌ stage_runs 变化!"; FAIL=$((FAIL+1)); }
[ "$O1" = "$O2" ] && { echo "✅ outbox 无变化"; PASS=$((PASS+1)); } || { echo "❌ outbox 变化!"; FAIL=$((FAIL+1)); }

echo ""
echo "=== 5. 最终 PG 状态 ==="
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT run_id,status,current_stage,verdict FROM task_runs WHERE run_id='${RUN_ID}';" 2>&1 | grep -v "^$"
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT stage,attempt,status FROM stage_runs WHERE run_id='${RUN_ID}' ORDER BY id;" 2>&1 | grep -v "^$"

echo ""
echo "═══════════════════════════════════════════════"
echo "  PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "  ✅ 崩溃恢复测试通过(Controller 重启后无重复派发)" || echo "  ❌ 有重复"
echo "═══════════════════════════════════════════════"
