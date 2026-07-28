#!/bin/bash
# m3b-b4c1_5-hardening.sh — B4c.1.5 修正(expiry deadline guard + L2_EXPIRY_BATCH 参数化)。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c-hardening
mkdir -p "$EV"; rm -f "$EV"/b4c15-*.txt "$EV"/b4c15-*.out
OUT="$EV/b4c15-test.out"; : > "$OUT"
log(){ echo "$*" | tee -a "$OUT"; }
PASS=0; FAIL=0
ok(){ log "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ log "  ❌ $1"; FAIL=$((FAIL+1)); }
TS=$$
CTRL=/home/ngh/.config/mergepilot/controller.env
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=mergepilot_audit
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
DRUN(){ docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
  -e GATEWAY_URL="${2:-http://policy-gw-e2e:8083}" -e COORDINATOR_TOKEN="dummy" -e L2_MERGE_ENABLED=0 \
  -e L2_GW_TIMEOUT=5 -e L2_EXPIRY_BATCH="${3:-50}" \
  mergepilot-controller:latest python3 -c "$1" 2>&1 | grep -vE "^Unable|^[0-9a-f]{12}: "; }
cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'h15-%'; DELETE FROM approvals WHERE run_id LIKE 'h15-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'h15-%'; DELETE FROM task_runs WHERE run_id LIKE 'h15-%';" >/dev/null 2>&1 || true; }
trap '{ cleanup_db; docker start mergepilot-controller >/dev/null 2>&1 || true; } EXIT'
log "═══════════════════════════════════════════════"; log "  B4c.1.5(fixture)"; log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker stop mergepilot-controller >/dev/null 2>&1 || true
cleanup_db
# 建一张过期 PENDING 票(approval_expires_at < now)
mk_expired_pending(){ local RUN="$1"
  PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','nghqqa/x',0,'l2_awaiting_approval',TRUE);" >/dev/null
  PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-$RUN','$RUN','nghqqa/x',0,'fix/x','main','0000000000000000000000000000000000000001');" >/dev/null
  local PAY='{"owner":"nghqqa","repo":"x","pullNumber":0,"commit_title":"x","merge_method":"squash"}'
  local AH=$(PSQL "SELECT l2_create_ticket('bnd-$RUN','merge','$PAY'::jsonb,'$(echo -n $PAY | sha256sum | cut -c1-64)',24,1);" 2>/dev/null)
  PSQL "UPDATE approvals SET approval_expires_at = now() - interval '1 hour' WHERE ticket_id='$AH';" >/dev/null
  echo "$AH"; }

# ═══ 1. expired-deadline 跳过 expiry(票仍 PENDING)═══
log ""; log "=== 1. expired deadline → expiry 跳过 ==="
RUN1=h15-dl-$TS; TKT1=$(mk_expired_pending "$RUN1")
if [ -z "$TKT1" ]; then bad "建过期 PENDING 票失败"; else
  DRUN "import time, controller
controller.reconcile_l2(deadline=time.monotonic()-10)" >/dev/null
  ST1=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  [ "$ST1" = "PENDING" ] && ok "deadline 到期 → expiry 跳过(票仍 PENDING)" || bad "expiry 未跳过: $ST1"
  # 正常 deadline → expiry 处理
  DRUN "import time, controller
controller.reconcile_l2(deadline=time.monotonic()+60)" >/dev/null
  ST1b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  [ "$ST1b" = "EXPIRED" ] && ok "正常 deadline → expiry 处理(票 EXPIRED)" || bad "expiry 未处理: $ST1b"
fi

# ═══ 2. L2_EXPIRY_BATCH=1 → 仅 1/3 过期票处理 ═══
log ""; log "=== 2. L2_EXPIRY_BATCH=1 → 仅 1/3 处理 ==="
cleanup_db
TKTS=()
for k in 1 2 3; do RR=h15-b$k-$TS; TT=$(mk_expired_pending "$RR"); TKTS+=("$TT"); done
if [ "${#TKTS[@]}" -ne 3 ]; then bad "建票不足(${#TKTS[@]}/3)"; else
  DRUN "import time, controller
controller.reconcile_l2(deadline=time.monotonic()+60)" "" 1 >/dev/null
  EXP=$(PSQL "SELECT count(*) FROM approvals WHERE ticket_id IN ('${TKTS[0]}','${TKTS[1]}','${TKTS[2]}') AND status='EXPIRED';")
  [ "$EXP" = "1" ] && ok "L2_EXPIRY_BATCH=1 → 仅 1/3 过期票处理" || bad "处理数=$EXP(应 1)"
fi

# ═══ 3. fixture 硬门 ═══
log ""; log "=== 3. fixture 硬门 ==="
cleanup_db
[ "$(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 200 --json number -q 'length' 2>/dev/null)" = "0" ] && ok "fixture: 0 PR" || bad "fixture PR"
[ "$(gh.exe api "repos/$(e2e_repo)/branches" --paginate -q '[.[].name]|length' 2>/dev/null)" = "1" ] && ok "fixture: 仅 main" || bad "fixture branches"
set +e; grep -rniE "PGPASSWORD|APPROVER_PASS|token=[A-Za-z0-9]{16}" "$OUT" > "$EV/credential-scan-b4c15.txt" 2>/dev/null
grep -vE "ROLE_TOKENS|COORDINATOR_TOKEN=" "$EV/credential-scan-b4c15.txt" > "$EV/.csf" 2>/dev/null || true
if [ -s "$EV/.csf" ]; then bad "泄漏?"; else printf 'B4c.1.5:无泄漏。\n' > "$EV/credential-scan-b4c15.txt"; ok "无泄漏"; fi; rm -f "$EV/.csf"
trap 'docker start mergepilot-controller >/dev/null 2>&1 || true' EXIT
sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$OUT" 2>/dev/null || true
log ""; log "═══════════════════════════════════════════════"; log "  B4c.1.5: PASS=$PASS FAIL=$FAIL"; log "═══════════════════════════════════════════════"
EXPECTED=6
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED" ]; then log "  全部 $EXPECTED 项通过"; exit 0; else log "  失败(期望 $EXPECTED,PASS=$PASS FAIL=$FAIL)"; exit 1; fi
