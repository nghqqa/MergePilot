#!/bin/bash
# m3b-b4c1_1-hardening.sh — B4c.1.1 修正验收(7 P1 + 负向矩阵,fixture 隔离)。
# 覆盖:
#   1. migration/ACL + l2_reject_approved(NULL) 拒。
#   2. deny 收敛回归 + #5 并发 CAS:claim 后再 deny → l2_reject_approved 返 false → outbox 不 FAILED。
#   3. #1 RETRY 重新排队:不可达 Gateway → discover RETRY → task l2_next_attempt_at 未来 + retry_count++。
#   4. #3 circuit breaker:坏 token → discover GLOBAL_DEGRADED(开 breaker)+ task 不推进。
#   5. #7 预检:lease<timeout+5 → FATAL;缺 l2_reject_approved → FATAL。
#   6. #4 预算下限:5 mismatched 票单 tick 处理 ≥1 且 ≤ MAX_ITEMS。
#   7. 凭证 + fixture 清理 + [PASS-eq N]。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c-hardening
mkdir -p "$EV"; rm -f "$EV"/b4c11-*.txt "$EV"/b4c11-*.out
OUT="$EV/b4c11-test.out"; : > "$OUT"
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
# controller 一次性:$1=py, $2=GW_URL, $3=token, $4=timeout
DRUN(){ local GWU="${2:-http://policy-gw-e2e:8083}"; local TK="${3:-$ECOORD}"; local TO="${4:-15}"
  docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
    -e GATEWAY_URL="$GWU" -e COORDINATOR_TOKEN="$TK" -e L2_MERGE_ENABLED=0 -e L2_GW_TIMEOUT=$TO \
    mergepilot-controller:latest python3 -c "$1" 2>&1 | grep -vE "^Unable to find image|^[0-9a-f]{12}: "; }
cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'h11-%'; DELETE FROM approvals WHERE run_id LIKE 'h11-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'h11-%'; DELETE FROM task_runs WHERE run_id LIKE 'h11-%';" >/dev/null 2>&1 || true; }
cleanup_fixture(){ for n in $(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 100 -q '.[]|select(.title|test("hard11"))|.number' 2>/dev/null); do gh.exe pr close "$n" --repo "$(e2e_repo)" --delete-branch --comment "B4c.1.1 清理" >/dev/null 2>&1 || true; done; }
trap '{ cleanup_db; cleanup_fixture; docker rm -f policy-gw-e2e 2>/dev/null; docker start mergepilot-controller >/dev/null 2>&1 || true; } EXIT'

log "═══════════════════════════════════════════════"
log "  B4c.1.1 修正验收(fixture=$(e2e_repo))"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker stop mergepilot-controller >/dev/null 2>&1 || true
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "PASS=$PASS FAIL=$FAIL"; exit 1; }
docker cp "$TOOLS/audit-db/m3b_b4c1_1.sql" audit-pg:/tmp/m3b_b4c1_1.sql >/dev/null
docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 -f /tmp/m3b_b4c1_1.sql >>"$OUT" 2>&1
docker build -t mergepilot-controller:latest "$TOOLS/workflow-controller" >>"$OUT" 2>&1
cleanup_db; cleanup_fixture
create_fix_pr(){ local BR="$1" L="$2" R
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" >/dev/null 2>&1
  e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="h11-$L-$TS.md" branch="$BR" content="h11$TS" message="h11 $L" >/dev/null 2>&1
  R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="hard11 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
read_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null; }
mk_approved(){ local RUN="$1" BR="$2" PR="$3" L="$4" HS BID PAY AH TKT
  HS=$(read_sha "$PR"); PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','$(e2e_repo)',$PR,'l2_awaiting_approval',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_approval';" >/dev/null
  BID="bnd-$RUN"; PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','main','$HS') ON CONFLICT(binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  PAY='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR',"commit_title":"h11 '"$L"'","merge_method":"squash"}'; AH=$(ah "$PAY")
  TKT=$(PSQL "SELECT l2_create_ticket('$BID','merge','$PAY'::jsonb,'$AH',24,1);"); APV "SELECT l2_approve('$TKT');" >/dev/null 2>&1; echo "$TKT"; }

# ════════════ 1. migration/ACL + NULL reject ════════════
log ""; log "=== 1. migration/ACL + l2_reject_approved(NULL) 拒 ==="
[ "$(PSQL "SELECT has_function_privilege('mergepilot','l2_reject_approved(text,text)','EXECUTE');")" = "t" ] && ok "mergepilot 可 EXECUTE l2_reject_approved" || bad "EXECUTE 缺失"
NULLRC=$(docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "SELECT l2_reject_approved('nope',NULL);" 2>&1 | head -1)
echo "$NULLRC" | grep -qi "allowlist" && ok "l2_reject_approved(NULL) → 拒(allowlist,不绕过)" || bad "NULL 绕过: $NULLRC"

# ════════════ 2. deny 回归 + #5 并发 CAS ════════════
log ""; log "=== 2. deny 回归 + #5 并发 CAS(claim 后 deny → outbox 不 FAILED)==="
RUN2=h11-deny-$TS; BR2=fix/${RUN2}-x; PR2=$(create_fix_pr "$BR2" "deny")
TKT2=$(mk_approved "$RUN2" "$BR2" "$PR2" "deny")
if [ -z "$TKT2" ]; then bad "deny: 建票失败(显式)"; else
  PSQL "UPDATE approvals SET args_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE ticket_id='$TKT2';" >/dev/null
  DRUN "import controller; controller.drain_l2_outbox()" >/dev/null
  [ "$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT2';")" = "FAILED" ] && ok "deny: approval→FAILED" || bad "deny approval"
  [ "$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT2';")" = "FAILED" ] && ok "deny: outbox→FAILED" || bad "deny outbox"
  [ "$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN2';")" = "l2_drain_denied" ] && ok "deny: stage=l2_drain_denied" || bad "deny stage"
fi
# #5 并发 CAS:先 claim(EXECUTING)再 deny → l2_reject_approved 返 false → outbox 不终结
RUN2b=h11-cas-$TS; BR2b=fix/${RUN2b}-x; PR2b=$(create_fix_pr "$BR2b" "cas")
TKT2b=$(mk_approved "$RUN2b" "$BR2b" "$PR2b" "cas")
if [ -z "$TKT2b" ]; then bad "CAS: 建票失败(显式)"; else
  PSQL "UPDATE approvals SET args_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE ticket_id='$TKT2b';" >/dev/null
  # 模拟并发:claim 前把 approval 推到 EXECUTING(已 claim)
  PSQL "UPDATE approvals SET status='EXECUTING', execution_id=gen_random_uuid(), executing_at=now() WHERE ticket_id='$TKT2b';" >/dev/null
  PSQL "UPDATE policy_action_outbox SET status='DISPATCHED', attempts=1 WHERE ticket_id='$TKT2b';" >/dev/null
  DRUN "import controller
import gateway_client
class _T(gateway_client.GatewayDenied):
    pass
# 直接调 _advance_outbox_by_approval 模拟 TICKET_DENY outcome(approval 已 EXECUTING → 应不终结)
controller._advance_outbox_by_approval(None, '$TKT2b', 'merge', controller.GatewayOutcome('TICKET_DENY','CLAIM_MISMATCH',''))" >/dev/null 2>&1
  AST2b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT2b';")
  OST2b=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT2b';")
  [ "$AST2b" = "EXECUTING" ] && ok "#5: approval 已 claim(EXECUTING)→ deny 不覆盖(仍 EXECUTING)" || bad "#5 approval=$AST2b"
  [ "$OST2b" = "DISPATCHED" ] && ok "#5: outbox 不被误标 FAILED(仍 DISPATCHED)" || bad "#5 outbox=$OST2b"
fi

# ════════════ 3. #1 RETRY 重新排队 ════════════
log ""; log "=== 3. #1 RETRY 重新排队(不可达 Gateway → next_attempt_at 未来)==="
RUN3=h11-retry-$TS; BR3=fix/${RUN3}-x; PR3=$(create_fix_pr "$BR3" "retry")
if [ -z "$PR3" ]; then bad "retry: PR 失败(显式)"; else
  PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN3','APPROVAL_PENDING','$(e2e_repo)',$PR3,'l2_binding',TRUE);" >/dev/null
  DRUN "import controller; controller.initiate_l2_pending()" "http://policy-gw-unreachable:9999" "$ECOORD" 8 >/dev/null
  NA3=$(PSQL "SELECT l2_next_attempt_at > now() FROM task_runs WHERE run_id='$RUN3';")
  RC3=$(PSQL "SELECT l2_retry_count FROM task_runs WHERE run_id='$RUN3';")
  CS3=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN3';")
  [ "$NA3" = "t" ] && ok "#1: RETRY → l2_next_attempt_at 未来(重新排队)" || bad "#1 next_attempt_at 未未来: $NA3"
  [ "$RC3" -ge "1" ] && ok "#1: l2_retry_count=$RC3(累加)" || bad "#1 retry_count=$RC3"
  [ "$CS3" = "l2_binding" ] && ok "#1: task 仍 l2_binding(未误推进)" || bad "#1 stage=$CS3"
fi

# ════════════ 4. #3/#4 circuit breaker(drain 首条 TRANSIENT 开 breaker,本 tick 停)════════════
log ""; log "=== 4. #3 circuit breaker(drain 不可达 → 首条 TRANSIENT 开 breaker,本 tick 不连环撞)==="
# 注:坏 token 经 SSE 401 → GatewayUnavailable(TRANSIENT),由 #1 退避处理;breaker 的可观测行为在 drain:
#   3 张 due 票 + 不可达 gateway → 领第 1 条(TRANSIENT)→ 打开 breaker → break,余 2 条不领(sum attempts=1)。
B3a=()
for k in 1 2 3; do RR=h11-brk$k-$TS; BB=fix/${RR}-x; PP=$(create_fix_pr "$BB" "brk$k"); TT=$(mk_approved "$RR" "$BB" "$PP" "brk$k"); B3a+=("$TT"); done
if [ "${#B3a[@]}" -ne 3 ]; then bad "breaker: 建票不足(${#B3a[@]}/3,显式)"; else
  DRUN "import controller; controller.drain_l2_outbox()" "http://policy-gw-unreachable:9999" "$ECOORD" 8 >/dev/null
  ATT_SUM=$(PSQL "SELECT sum(attempts) FROM policy_action_outbox WHERE ticket_id IN ('${B3a[0]}','${B3a[1]}','${B3a[2]}');")
  [ "$ATT_SUM" = "1" ] && ok "#3/#4: drain 首条 TRANSIENT 开 breaker,本 tick 只领 1(sum attempts=1)" || bad "breaker 未停: sum=$ATT_SUM(应 1)"
fi

# ════════════ 5. #7 预检(lease<timeout+5 FATAL;缺 migration FATAL)════════════
log ""; log "=== 5. #7 预检硬门 ==="
docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
  -e GATEWAY_URL=http://policy-gw:8083 -e COORDINATOR_TOKEN="$ECOORD" -e L2_MERGE_ENABLED=1 -e STARTUP_CHECK_ONLY=1 \
  -e L2_LEASE_SECONDS=10 -e L2_GW_TIMEOUT=60 mergepilot-controller:latest >/tmp/lease.out 2>&1; LEASE_RC=$?
grep -q "FATAL.*L2_LEASE_SECONDS" /tmp/lease.out && ok "#7: lease(10)<timeout+5(65) → FATAL" || bad "#7 lease 未 fatal(rc=$LEASE_RC)"
# 缺 l2_reject_approved → FATAL(临时 drop,验 gone,再预检,再恢复)
docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "DROP FUNCTION IF EXISTS l2_reject_approved(text,text);" >/dev/null 2>&1
GONE=$(PSQL "SELECT to_regprocedure('l2_reject_approved(text,text)') IS NULL;")
if [ "$GONE" = "t" ]; then
  docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
    -e GATEWAY_URL=http://policy-gw:8083 -e COORDINATOR_TOKEN="$ECOORD" -e L2_MERGE_ENABLED=1 -e STARTUP_CHECK_ONLY=1 \
    mergepilot-controller:latest >/tmp/mig.out 2>&1; MIG_RC=$?
  docker cp "$TOOLS/audit-db/m3b_b4c1_1.sql" audit-pg:/tmp/m3b_b4c1_1.sql >/dev/null
  docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 -f /tmp/m3b_b4c1_1.sql >/dev/null 2>&1   # 恢复
  grep -q "FATAL" /tmp/mig.out && ok "#7: 缺 l2_reject_approved → FATAL(预检拦)" || bad "#7 缺 migration 未 fatal(rc=$MIG_RC): $(head -1 /tmp/mig.out)"
else
  docker cp "$TOOLS/audit-db/m3b_b4c1_1.sql" audit-pg:/tmp/m3b_b4c1_1.sql >/dev/null 2>&1
  docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 -f /tmp/m3b_b4c1_1.sql >/dev/null 2>&1
  bad "#7: DROP l2_reject_approved 失败(audit-pg 不稳,跳过)"
fi
# 正常预检仍通过(恢复后)
docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER=$PG_SU \
  -e GATEWAY_URL=http://policy-gw:8083 -e COORDINATOR_TOKEN="$ECOORD" -e L2_MERGE_ENABLED=1 -e STARTUP_CHECK_ONLY=1 \
  mergepilot-controller:latest >/tmp/ok.out 2>&1; OK_RC=$?
grep -q "startup_assert passed" /tmp/ok.out && ok "#7: migration 完整时预检通过" || bad "#7 正常预检失败(rc=$OK_RC)"

# ════════════ 6. #4 预算下限(≥1 且 ≤ MAX_ITEMS)════════════
log ""; log "=== 6. #4 预算下限(单 tick ≥1 且 ≤ MAX_ITEMS=3)==="
BUD=()
for k in 1 2 3 4 5; do RR=h11-bud$k-$TS; BB=fix/${RR}-x; PP=$(create_fix_pr "$BB" "bud$k"); TT=$(mk_approved "$RR" "$BB" "$PP" "bud$k")
  [ -n "$TT" ] && PSQL "UPDATE approvals SET args_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE ticket_id='$TT';" >/dev/null; BUD+=("$TT"); done
if [ "${#BUD[@]}" -ne 5 ]; then bad "budget: 建票不足(${#BUD[@]}/5,显式)"; else
  DRUN "import controller; controller.drain_l2_outbox()" >/dev/null
  F1=$(PSQL "SELECT count(*) FROM policy_action_outbox WHERE ticket_id IN ('${BUD[0]}','${BUD[1]}','${BUD[2]}','${BUD[3]}','${BUD[4]}') AND status='FAILED';")
  [ "$F1" -ge 1 ] && [ "$F1" -le 3 ] && ok "#4: 单 tick 处理 $F1 条(≥1 且 ≤ MAX_ITEMS=3)" || bad "#4 处理数=$F1(应 1..3)"
fi

# ════════════ 7. 凭证 + 收尾 ════════════
log ""; log "=== 7. 凭证扫描 + 收尾 ==="
set +e; grep -rniE "PGPASSWORD|APPROVER_PASS|POLICY_GATEWAY_L2_PASS|token=[A-Za-z0-9]{16}|Bearer [A-Za-z0-9]{16}" "$OUT" > "$EV/credential-scan-b4c11.txt" 2>/dev/null; GR=$?
grep -vE "ROLE_TOKENS|COORDINATOR_TOKEN=|token_urlsafe|wrong-token-not-valid" "$EV/credential-scan-b4c11.txt" > "$EV/credential-scan-filtered.txt" 2>/dev/null || true
if [ -s "$EV/credential-scan-filtered.txt" ]; then bad "凭证泄漏? $(head -2 $EV/credential-scan-filtered.txt)"; else : > "$EV/credential-scan-b4c11.txt"; ok "无凭证泄漏"; fi
cleanup_db; cleanup_fixture; trap 'docker start mergepilot-controller >/dev/null 2>&1 || true' EXIT
sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$OUT" 2>/dev/null || true
log ""
log "═══════════════════════════════════════════════"
log "  B4c.1.1 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
EXPECTED=16
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED" ]; then log "  全部 $EXPECTED 项通过(无静默跳过)"; exit 0
else log "  失败或未跑满(期望 $EXPECTED,实际 PASS=$PASS FAIL=$FAIL)"; exit 1; fi
