#!/bin/bash
# m3b-b4c1_6-hardening.sh — B4c.1.6: mid-batch deadline guard(expiry/stranded 批内检查)。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c-hardening
mkdir -p "$EV"; rm -f "$EV"/b4c16-*.txt "$EV"/b4c16-*.out
OUT="$EV/b4c16-test.out"; : > "$OUT"
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
  -e GATEWAY_URL="http://dummy:1" -e COORDINATOR_TOKEN="x" -e L2_MERGE_ENABLED=0 -e L2_GW_TIMEOUT=5 -e L2_EXPIRY_BATCH="${3:-50}" \
  mergepilot-controller:latest python3 -c "$1" 2>&1 | grep -vE "^Unable|^[0-9a-f]{12}: "; }
cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'h16-%'; DELETE FROM approvals WHERE run_id LIKE 'h16-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'h16-%'; DELETE FROM task_runs WHERE run_id LIKE 'h16-%';" >/dev/null 2>&1 || true; }
trap '{ cleanup_db; docker start mergepilot-controller >/dev/null 2>&1 || true; } EXIT'
log "═══════════════════════════════════════════════"; log "  B4c.1.6(fixture)"; log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker stop mergepilot-controller >/dev/null 2>&1 || true
cleanup_db
mk_ep(){ local RUN="$1"
  PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','nghqqa/x',0,'l2_awaiting_approval',TRUE);" >/dev/null
  PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-$RUN','$RUN','nghqqa/x',0,'fix/x','main','0000000000000000000000000000000000000001');" >/dev/null
  local PAY='{"owner":"nghqqa","repo":"x","pullNumber":0,"commit_title":"x","merge_method":"squash"}'
  local AH=$(PSQL "SELECT l2_create_ticket('bnd-$RUN','merge','$PAY'::jsonb,'$(echo -n $PAY | sha256sum | cut -c1-64)',24,1);" 2>/dev/null)
  PSQL "UPDATE approvals SET approval_expires_at = now() - interval '1 hour' WHERE ticket_id='$AH';" >/dev/null; }

# ═══ 1. mid-batch deadline guard(monkeypatch _budget_exhausted:前 3 调通过,第 4 调断)═══
log ""; log "=== 1. mid-batch deadline guard(monkeypatch 确定性) ==="
for k in 1 2 3 4 5; do mk_ep "h16-mid$k-$TS"; done
# Monkeypatch: calls 1-3 = False(entry+section+item1 通过);call 4+ = True(item2 break)
MID_OUT=$(DRUN "import time, controller
_n = [0]
def _mock(dl):
    _n[0] += 1
    return _n[0] > 3   # 前 3 次通过,第 4 次起 exhausted
controller._budget_exhausted = _mock
controller.reconcile_l2(deadline=time.monotonic()+60)
print(f'MOCK_CALLS={_n[0]}')" "" 500)
echo "$MID_OUT" >>"$OUT"
EXP1=$(PSQL "SELECT count(*) FROM approvals WHERE run_id LIKE 'h16-mid%-%' AND status='EXPIRED';")
TOTAL=$(PSQL "SELECT count(*) FROM approvals WHERE run_id LIKE 'h16-mid%-%';")
logf(){ echo "$*" >>"$OUT"; }
logf "  mid-batch: expired=$EXP1/$TOTAL (monkeypatch: 3 pass then break)"
[ "$EXP1" -gt 0 ] && [ "$EXP1" -lt "$TOTAL" ] && ok "mid-batch deadline:仅 $EXP1/$TOTAL 过期(0<expired<total,break 确定命中)" || bad "mid-batch: expired=$EXP1/$TOTAL (应 0<x<total)"

# 剩余在正常 deadline(monkeypatch 恢复)下处理
DRUN "import time, controller
controller.reconcile_l2(deadline=time.monotonic()+60)" "" 500 >/dev/null
EXP2=$(PSQL "SELECT count(*) FROM approvals WHERE run_id LIKE 'h16-mid%-%' AND status='EXPIRED';")
[ "$EXP2" = "$TOTAL" ] && ok "正常 deadline:剩余 $((TOTAL-EXP1)) 项在下一 tick 处理(累计 $EXP2/$TOTAL)" || bad "剩余未处理: $EXP2/$TOTAL"

# ═══ 2. fixture 硬门 ═══
log ""; log "=== 2. fixture 硬门 ==="
cleanup_db
[ "$(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 200 --json number -q 'length' 2>/dev/null)" = "0" ] && ok "fixture: 0 PR" || bad "fixture PR"
[ "$(gh.exe api "repos/$(e2e_repo)/branches" --paginate -q '[.[].name]|length' 2>/dev/null)" = "1" ] && ok "fixture: 仅 main" || bad "fixture branches"
set +e; grep -rniE "PGPASSWORD|APPROVER_PASS|token=[A-Za-z0-9]{16}" "$OUT" > "$EV/credential-scan-b4c16.txt" 2>/dev/null
grep -vE "ROLE_TOKENS|COORDINATOR_TOKEN=" "$EV/credential-scan-b4c16.txt" > "$EV/.csf" 2>/dev/null || true
if [ -s "$EV/.csf" ]; then bad "泄漏?"; else printf 'B4c.1.6:无泄漏。\n' > "$EV/credential-scan-b4c16.txt"; ok "无泄漏"; fi; rm -f "$EV/.csf"
trap 'docker start mergepilot-controller >/dev/null 2>&1 || true' EXIT
sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$OUT" 2>/dev/null || true
log ""; log "═══════════════════════════════════════════════"; log "  B4c.1.6: PASS=$PASS FAIL=$FAIL"; log "═══════════════════════════════════════════════"
EXPECTED=5
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED" ]; then log "  全部 $EXPECTED 项通过"; exit 0; else log "  失败(期望 $EXPECTED,PASS=$PASS FAIL=$FAIL)"; exit 1; fi
