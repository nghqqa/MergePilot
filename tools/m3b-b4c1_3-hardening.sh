#!/bin/bash
# m3b-b4c1_3-hardening.sh — B4c.1.3 修正验收(共享预算/3字段CAS/breaker恢复,fixture)。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c-hardening
mkdir -p "$EV"; rm -f "$EV"/b4c13-*.txt "$EV"/b4c13-*.out
OUT="$EV/b4c13-test.out"; : > "$OUT"
log(){ echo "$*" | tee -a "$OUT"; }
PASS=0; FAIL=0
ok(){ log "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ log "  ❌ $1"; FAIL=$((FAIL+1)); }
TS=$$
CTRL=/home/ngh/.config/mergepilot/controller.env
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=mergepilot_audit
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' /home/ngh/.config/mergepilot/b4-roles.env | head -1 | cut -d= -f2-)
ECOORD=$(e2e_coordinator_token)
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
APV(){ docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "$1" 2>&1; }
ah(){ python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]),sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }
DRUN(){ docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
  -e GATEWAY_URL="${2:-http://policy-gw-e2e:8083}" -e COORDINATOR_TOKEN="$ECOORD" -e L2_MERGE_ENABLED=0 -e L2_GW_TIMEOUT=15 \
  mergepilot-controller:latest python3 -c "$1" 2>&1 | grep -vE "^Unable|^[0-9a-f]{12}: "; }
cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'h13-%'; DELETE FROM approvals WHERE run_id LIKE 'h13-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'h13-%'; DELETE FROM task_runs WHERE run_id LIKE 'h13-%';" >/dev/null 2>&1 || true; }
cleanup_fixture(){ for n in $(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 200 --json number,title -q '.[]|select(.title|test("hard13"))|.number' 2>/dev/null); do gh.exe pr close "$n" --repo "$(e2e_repo)" --delete-branch --comment "B4c.1.3 清理" >/dev/null 2>&1 || true; done
  for b in $(gh.exe api "repos/$(e2e_repo)/branches" --paginate -q '.[].name' 2>/dev/null | grep '^fix/h13'); do gh.exe api -X DELETE "repos/$(e2e_repo)/git/refs/heads/${b//\//%2F}" 2>/dev/null || true; done; }
trap '{ cleanup_db; cleanup_fixture; docker rm -f policy-gw-e2e 2>/dev/null; docker start mergepilot-controller >/dev/null 2>&1 || true; } EXIT'
log "═══════════════════════════════════════════════"
log "  B4c.1.3 修正验收(fixture=$(e2e_repo))"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker stop mergepilot-controller >/dev/null 2>&1 || true
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "PASS=$PASS FAIL=$FAIL"; exit 1; }
cleanup_db; cleanup_fixture
create_fix_pr(){ local BR="$1" L="$2" R
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" >/dev/null 2>&1
  e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="h13-$L-$TS.md" branch="$BR" content="h13$TS" message="h13 $L" >/dev/null 2>&1
  R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="hard13 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
read_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null; }
mk_approved(){ local RUN="$1" BR="$2" PR="$3" HS BID PAY AH TKT
  HS=$(read_sha "$PR"); PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','$(e2e_repo)',$PR,'l2_awaiting_approval',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_approval';" >/dev/null
  BID="bnd-$RUN"; PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','main','$HS') ON CONFLICT(binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  PAY='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR',"commit_title":"h13 '"$4"'","merge_method":"squash"}'; AH=$(ah "$PAY")
  TKT=$(PSQL "SELECT l2_create_ticket('$BID','merge','$PAY'::jsonb,'$AH',24,1);"); APV "SELECT l2_approve('$TKT');" >/dev/null 2>&1; echo "$TKT"; }

# ════════════ 1. 共享每-tick 预算 ════════════
log ""; log "=== 1. 共享每-tick 预算(budget=[2],3 approved → 仅 2 处理)==="
BUD=()
for k in 1 2 3; do RR=h13-bud$k-$TS; BB=fix/${RR}-x; PP=$(create_fix_pr "$BB" "bud$k"); TT=$(mk_approved "$RR" "$BB" "$PP" "bud$k"); BUD+=("$TT"); done
if [ "${#BUD[@]}" -ne 3 ]; then bad "共享预算: 建票不足(${#BUD[@]}/3,显式)"; else
  # 调 initiate+drain+reconcile 共享 budget=[2]
  DRUN "import controller
b=[2]
controller.initiate_l2_pending(None, b)
controller.drain_l2_outbox(None, b)
controller.reconcile_l2(None, b)" >/dev/null
  MERGED=$(PSQL "SELECT count(*) FROM task_runs WHERE run_id IN ('h13-bud1-$TS','h13-bud2-$TS','h13-bud3-$TS') AND status='MERGED';")
  [ "$MERGED" = "2" ] && ok "共享预算 budget=[2]: 仅 2 处理(MERGED=2),1 留待下 tick" || bad "共享预算: MERGED=$MERGED(应 2)"
fi

# ════════════ 2. deny 3 字段 CAS(approval_required=FALSE → 回滚)════════════
log ""; log "=== 2. deny 3 字段 CAS(approval_required=FALSE → deny 回滚)==="
RUN2=h13-cas-$TS; BR2=fix/${RUN2}-x; PR2=$(create_fix_pr "$BR2" "cas")
TKT2=$(mk_approved "$RUN2" "$BR2" "$PR2" "cas")
if [ -z "$TKT2" ]; then bad "deny CAS: 建票失败(显式)"; else
  PSQL "UPDATE approvals SET args_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE ticket_id='$TKT2';" >/dev/null
  PSQL "UPDATE task_runs SET approval_required=FALSE WHERE run_id='$RUN2';" >/dev/null   # 缺 approval_required
  PSQL "UPDATE policy_action_outbox SET status='DISPATCHED', attempts=1 WHERE ticket_id='$TKT2';" >/dev/null
  OID2=$(PSQL "SELECT id FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  DRUN "import controller
controller._advance_outbox_by_approval($OID2, '$TKT2', 'merge', controller.GatewayOutcome('TICKET_DENY','CLAIM_MISMATCH',''))" >/dev/null 2>&1
  AST2=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT2';")
  OST2=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  [ "$AST2" = "APPROVED" ] && ok "deny CAS: approval_required=FALSE → 回滚(approval 仍 APPROVED)" || bad "deny CAS approval=$AST2"
  [ "$OST2" = "DISPATCHED" ] && ok "deny CAS: outbox 不被标 FAILED(仍 DISPATCHED)" || bad "deny CAS outbox=$OST2"
fi

# ════════════ 3. fixture 硬门 + 凭证 ════════════
log ""; log "=== 3. fixture 硬门 + 凭证 ==="
cleanup_db; cleanup_fixture
FX_PR=$(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 200 --json number -q 'length' 2>/dev/null || echo "?")
FX_BR=$(gh.exe api "repos/$(e2e_repo)/branches" --paginate -q '[.[].name]|length' 2>/dev/null || echo "?")
[ "$FX_PR" = "0" ] && ok "fixture: 0 open PR" || bad "fixture open PR=$FX_PR"
[ "$FX_BR" = "1" ] && ok "fixture: 0 fix/ branch(仅 main)" || bad "fixture branches=$FX_BR"
set +e; grep -rniE "PGPASSWORD|APPROVER_PASS|POLICY_GATEWAY_L2_PASS|token=[A-Za-z0-9]{16}" "$OUT" > "$EV/credential-scan-b4c13.txt" 2>/dev/null
grep -vE "ROLE_TOKENS|COORDINATOR_TOKEN=" "$EV/credential-scan-b4c13.txt" > "$EV/.csf" 2>/dev/null || true
if [ -s "$EV/.csf" ]; then bad "凭证泄漏?"; else printf 'B4c.1.3:无泄漏。\n' > "$EV/credential-scan-b4c13.txt"; ok "无凭证泄漏"; fi
rm -f "$EV/.csf"
trap 'docker start mergepilot-controller >/dev/null 2>&1 || true' EXIT
sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$OUT" 2>/dev/null || true
log ""
log "═══════════════════════════════════════════════"
log "  B4c.1.3 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
EXPECTED=6
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED" ]; then log "  全部 $EXPECTED 项通过"; exit 0
else log "  失败或未跑满(期望 $EXPECTED,PASS=$PASS FAIL=$FAIL)"; exit 1; fi
