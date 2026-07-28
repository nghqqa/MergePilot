#!/bin/bash
# m3b-b4c1_2-hardening.sh — B4c.1.2 修正验收(3 P1 + 真并发/整轮预算/breaker,fixture 隔离)。
# 覆盖:
#   1. #1 真并发 deny CAS:approval APPROVED + task 已迁移(非 CAS)→ deny 不动 approval/outbox(回滚)。
#   2. #1 deny 正向回归:APPROVED + task CAS → FAILED/FAILED/HOLD(l2_drain_denied)。
#   3. #2 initiate breaker:不可达 Gateway → 首任务 GatewayUnavailable → 重排 + breaker(本 tick 停,余任务不动)。
#   4. #2 Denied 收敛:repo 不在 allowlist → discover GatewayDenied → task HOLD(不无限重试)。
#   5. #3 整轮预算:MAX_ITEMS=2 → 单 tick 处理 ≤2(共享 deadline)。
#   6. config 校验:MAX_ITEMS=0 → startup FATAL。
#   7. fixture 硬门:**0 open PR + 0 fix/ branch**;凭证 + [PASS-eq N]。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c-hardening
mkdir -p "$EV"; rm -f "$EV"/b4c12-*.txt "$EV"/b4c12-*.out
OUT="$EV/b4c12-test.out"; : > "$OUT"
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
# DRUN: $1=py $2=GW $3=token $4=timeout $5=MAX_ITEMS(env)
DRUN(){ local GWU="${2:-http://policy-gw-e2e:8083}"; local TK="${3:-$ECOORD}"; local TO="${4:-15}"; local MI="${5:-}"
  local E=("-e" "L2_GW_TIMEOUT=$TO"); [ -n "$MI" ] && E+=("-e" "L2_MAINTENANCE_MAX_ITEMS=$MI")
  docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
    -e GATEWAY_URL="$GWU" -e COORDINATOR_TOKEN="$TK" -e L2_MERGE_ENABLED=0 "${E[@]}" \
    mergepilot-controller:latest python3 -c "$1" 2>&1 | grep -vE "^Unable|^[0-9a-f]{12}: "; }
cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'h12-%'; DELETE FROM approvals WHERE run_id LIKE 'h12-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'h12-%'; DELETE FROM task_runs WHERE run_id LIKE 'h12-%';" >/dev/null 2>&1 || true; }
# B4c.1.2 修正:--json 必需(否则 -q 报错被忽略 → 不清理)
cleanup_fixture(){ for n in $(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 200 --json number,title -q '.[]|select(.title|test("hard12"))|.number' 2>/dev/null); do gh.exe pr close "$n" --repo "$(e2e_repo)" --delete-branch --comment "B4c.1.2 清理" >/dev/null 2>&1 || true; done
  for b in $(gh.exe api "repos/$(e2e_repo)/branches" --paginate -q '.[].name' 2>/dev/null | grep '^fix/h12'); do gh.exe api -X DELETE "repos/$(e2e_repo)/git/refs/heads/${b//\//%2F}" 2>/dev/null || true; done; }
trap '{ cleanup_db; cleanup_fixture; docker rm -f policy-gw-e2e 2>/dev/null; docker start mergepilot-controller >/dev/null 2>&1 || true; } EXIT'

log "═══════════════════════════════════════════════"
log "  B4c.1.2 修正验收(fixture=$(e2e_repo))"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker stop mergepilot-controller >/dev/null 2>&1 || true
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "PASS=$PASS FAIL=$FAIL"; exit 1; }
docker build -t mergepilot-controller:latest "$TOOLS/workflow-controller" >>"$OUT" 2>&1
cleanup_db; cleanup_fixture
create_fix_pr(){ local BR="$1" L="$2" R
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" >/dev/null 2>&1
  e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="h12-$L-$TS.md" branch="$BR" content="h12$TS" message="h12 $L" >/dev/null 2>&1
  R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="hard12 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
read_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null; }
mk_approved(){ local RUN="$1" BR="$2" PR="$3" L="$4" HS BID PAY AH TKT
  HS=$(read_sha "$PR"); PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','$(e2e_repo)',$PR,'l2_awaiting_approval',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_approval';" >/dev/null
  BID="bnd-$RUN"; PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','main','$HS') ON CONFLICT(binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  PAY='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR',"commit_title":"h12 '"$L"'","merge_method":"squash"}'; AH=$(ah "$PAY")
  TKT=$(PSQL "SELECT l2_create_ticket('$BID','merge','$PAY'::jsonb,'$AH',24,1);"); APV "SELECT l2_approve('$TKT');" >/dev/null 2>&1; echo "$TKT"; }
# 直接调 _advance_outbox_by_approval 模拟 deny outcome(用于并发 CAS 测试,不走真 drain)
deny_advance(){ local TKT="$1" OID="$2" RUN="$3"
  docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU -e L2_MERGE_ENABLED=0 \
    mergepilot-controller:latest python3 -c "
import controller
controller._advance_outbox_by_approval($OID, '$TKT', 'merge', controller.GatewayOutcome('TICKET_DENY','CLAIM_MISMATCH',''))
" >/dev/null 2>&1; }

# ════════════ 1. #1 真并发 deny CAS ════════════
log ""; log "=== 1. #1 deny 正向 + 真并发 CAS ==="
# 1a. 正向:APPROVED + task CAS → FAILED/FAILED/HOLD
RUN1a=h12-deny-$TS; BR1a=fix/${RUN1a}-x; PR1a=$(create_fix_pr "$BR1a" "deny")
TKT1a=$(mk_approved "$RUN1a" "$BR1a" "$PR1a" "deny")
if [ -z "$TKT1a" ]; then bad "deny 正向: 建票失败(显式)"; else
  PSQL "UPDATE approvals SET args_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE ticket_id='$TKT1a';" >/dev/null
  DRUN "import controller; controller.drain_l2_outbox()" >/dev/null
  [ "$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1a';")" = "FAILED" ] && ok "deny 正向: approval→FAILED" || bad "deny 正向 approval"
  [ "$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN1a';")" = "l2_drain_denied" ] && ok "deny 正向: task HOLD(l2_drain_denied)" || bad "deny 正向 stage"
fi
# 1b. 真并发:approval APPROVED 但 task 已脱离 CAS(MERGED)→ deny 应回滚,approval/outbox 不动
RUN1b=h12-cas-$TS; BR1b=fix/${RUN1b}-x; PR1b=$(create_fix_pr "$BR1b" "cas")
TKT1b=$(mk_approved "$RUN1b" "$BR1b" "$PR1b" "cas")
if [ -z "$TKT1b" ]; then bad "并发 CAS: 建票失败(显式)"; else
  PSQL "UPDATE approvals SET args_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE ticket_id='$TKT1b';" >/dev/null
  PSQL "UPDATE policy_action_outbox SET status='DISPATCHED', attempts=1 WHERE ticket_id='$TKT1b';" >/dev/null
  OID1b=$(PSQL "SELECT id FROM policy_action_outbox WHERE ticket_id='$TKT1b';")
  # 关键:task 已 MERGED(脱离 APPROVAL_PENDING/l2_awaiting_approval)但 approval 仍 APPROVED
  PSQL "UPDATE task_runs SET status='MERGED', current_stage='l2_done' WHERE run_id='$RUN1b';" >/dev/null
  deny_advance "$TKT1b" "$OID1b" "$RUN1b"
  AST1b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1b';")
  OST1b=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT1b';")
  [ "$AST1b" = "APPROVED" ] && ok "#1 并发: task 已 MERGED → deny 回滚,approval 仍 APPROVED(不动)" || bad "#1 并发 approval=$AST1b(应 APPROVED)"
  [ "$OST1b" = "DISPATCHED" ] && ok "#1 并发: outbox 不被误标 FAILED(仍 DISPATCHED)" || bad "#1 并发 outbox=$OST1b"
fi

# ════════════ 2. #2 initiate breaker(不可达 → GatewayUnavailable → 重排 + breaker)════════════
log ""; log "=== 2. #2 initiate breaker(不可达 → 首任务重排 + breaker,本 tick 停)==="
for k in 1 2 3; do
  RR=h12-brk$k-$TS; BB=fix/${RR}-x; PP=$(create_fix_pr "$BB" "brk$k")
  PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RR','APPROVAL_PENDING','$(e2e_repo)',$PP,'l2_binding',TRUE);" >/dev/null
  eval "BRK_T$k=$RR"
done
DRUN "import controller; controller.initiate_l2_pending()" "http://policy-gw-unreachable:9999" "$ECOORD" 8 >/dev/null
NA1=$(PSQL "SELECT l2_next_attempt_at > now() FROM task_runs WHERE run_id='${BRK_T1}';")
RC1=$(PSQL "SELECT l2_retry_count FROM task_runs WHERE run_id='${BRK_T1}';")
NA2=$(PSQL "SELECT l2_next_attempt_at > now() FROM task_runs WHERE run_id='${BRK_T2}';")
[ "$NA1" = "t" ] && [ "$RC1" -ge 1 ] && ok "#2: 首任务 GatewayUnavailable → 重排(next_attempt_at 未来,retry_count=$RC1)" || bad "#2 首任务未重排: NA=$NA1 RC=$RC1"
[ "$NA2" = "f" ] && ok "#2: breaker 打开 → 后续任务不动(next_attempt_at 仍=now,未连环撞)" || bad "#2 后续任务被处理(应停)"

# ════════════ 3. #2 Denied 收敛(repo 不在 allowlist → HOLD)════════════
log ""; log "=== 3. #2 Denied 收敛(repo 不在 allowlist → HOLD,不无限重试)==="
RUN3=h12-denyrepo-$TS
PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN3','APPROVAL_PENDING','nghqqa/NOT-ALLOWED-REPO',999,'l2_binding',TRUE);" >/dev/null
DRUN "import controller; controller.initiate_l2_pending()" >/dev/null
ST3=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN3';"); SG3=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN3';")
[ "$ST3" = "HOLD" ] && ok "#2: repo 不在 allowlist → GatewayDenied → task HOLD(收敛)" || bad "#2 repo deny: status=$ST3(应 HOLD)"
[ "$SG3" = "l2_binding_failed" ] && ok "#2: stage=l2_binding_failed" || bad "#2 stage=$SG3"

# ════════════ 4. #3 整轮预算(MAX_ITEMS=2 → 单 tick ≤2)════════════
log ""; log "=== 4. #3 整轮预算(MAX_ITEMS=2 → 单 drain ≤2)==="
BUD=()
for k in 1 2 3 4 5; do RR=h12-bud$k-$TS; BB=fix/${RR}-x; PP=$(create_fix_pr "$BB" "bud$k"); TT=$(mk_approved "$RR" "$BB" "$PP" "bud$k")
  [ -n "$TT" ] && PSQL "UPDATE approvals SET args_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE ticket_id='$TT';" >/dev/null; BUD+=("$TT"); done
if [ "${#BUD[@]}" -ne 5 ]; then bad "预算: 建票不足(${#BUD[@]}/5,显式)"; else
  DRUN "import controller; controller.drain_l2_outbox()" "http://policy-gw-e2e:8083" "$ECOORD" 15 2 >/dev/null
  F1=$(PSQL "SELECT count(*) FROM policy_action_outbox WHERE ticket_id IN ('${BUD[0]}','${BUD[1]}','${BUD[2]}','${BUD[3]}','${BUD[4]}') AND status='FAILED';")
  [ "$F1" -le 2 ] && [ "$F1" -ge 1 ] && ok "#3: MAX_ITEMS=2 → 单 tick 处理 $F1 条(≤2,预算硬边界)" || bad "#3 处理数=$F1(应 1..2)"
fi

# ════════════ 5. config 校验(MAX_ITEMS=0 → FATAL)════════════
log ""; log "=== 5. config 校验(MAX_ITEMS=0 → startup FATAL)==="
docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
  -e GATEWAY_URL=http://policy-gw:8083 -e COORDINATOR_TOKEN="$ECOORD" -e L2_MERGE_ENABLED=0 -e STARTUP_CHECK_ONLY=1 \
  -e L2_MAINTENANCE_MAX_ITEMS=0 mergepilot-controller:latest >/tmp/cfg.out 2>&1; CFG_RC=$?
grep -q "FATAL.*L2 配置非法\|FATAL.*MAX_ITEMS" /tmp/cfg.out && ok "config: MAX_ITEMS=0 → FATAL(防静默停摆)" || bad "config 未 fatal(rc=$CFG_RC): $(head -1 /tmp/cfg.out)"

# ════════════ 6. fixture 硬门 + 凭证 ════════════
log ""; log "=== 6. fixture 硬门 + 凭证 ==="
cleanup_db; cleanup_fixture
FX_PR=$(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 200 --json number -q 'length' 2>/dev/null || echo "?")
FX_BR=$(gh.exe api "repos/$(e2e_repo)/branches" --paginate -q '[.[].name]|length' 2>/dev/null || echo "?")
[ "$FX_PR" = "0" ] && ok "fixture 硬门: 0 open PR" || bad "fixture open PR=$FX_PR(应 0)"
[ "$FX_BR" = "1" ] && ok "fixture 硬门: 0 fix/ branch(仅 main)" || bad "fixture branches=$FX_BR(应 1=仅 main)"
set +e; grep -rniE "PGPASSWORD|APPROVER_PASS|POLICY_GATEWAY_L2_PASS|token=[A-Za-z0-9]{16}|Bearer [A-Za-z0-9]{16}" "$OUT" > "$EV/credential-scan-b4c12.txt" 2>/dev/null
grep -vE "ROLE_TOKENS|COORDINATOR_TOKEN=|token_urlserve|wrong-token" "$EV/credential-scan-b4c12.txt" > "$EV/.csf" 2>/dev/null || true
if [ -s "$EV/.csf" ]; then bad "凭证泄漏? $(head -2 $EV/.csf)"; else printf 'B4c.1.2 输出扫描:无 PGPASSWORD/PASS/token 泄漏。\n' > "$EV/credential-scan-b4c12.txt"; ok "无凭证泄漏"; fi
rm -f "$EV/.csf"
trap 'docker start mergepilot-controller >/dev/null 2>&1 || true' EXIT
sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$OUT" 2>/dev/null || true
log ""
log "═══════════════════════════════════════════════"
log "  B4c.1.2 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
EXPECTED=13
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED" ]; then log "  全部 $EXPECTED 项通过"; exit 0
else log "  失败或未跑满(期望 $EXPECTED,实际 PASS=$PASS FAIL=$FAIL)"; exit 1; fi
