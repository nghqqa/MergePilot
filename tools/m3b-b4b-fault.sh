#!/bin/bash
# m3b-b4b-fault.sh — B4b 故障路径覆盖。
# 修:FAULT_INJECT 需 /tmp/.test_mode(防生产后门);write_timeout 断言 upstream 被调用;
#     bad-audit 后断言 PR 仍 open + SHA 不变;audit-summary 查询修正;证据不计 PASS。
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b4b-fault.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

DIR=/home/ngh/.config/mergepilot
source "$DIR/audit-db.env"
AUDIT_DSN_VAL="postgresql://${PGW_AUDIT_USER}:${PGW_AUDIT_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
source "$DIR/b4-roles.env"
L2_DSN_VAL="postgresql://${POLICY_GATEWAY_L2_USER}:${POLICY_GATEWAY_L2_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
ROLE_TOKENS_VAL=$(cat "$DIR/role-tokens.json")
CTRL="$DIR/controller.env"
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=${PG_DB:-mergepilot_audit}; PG_SU=${PG_SU:-mergepilot}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
SU(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>&1; }
chash(){ python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]),sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }
GW(){ docker exec policy-gw python3 /tmp/probe-tools.py coordinator --call "${@}" 2>&1 | head -5; }
touch /tmp/.test-mode  # bind-mount 进 fault 容器

# fault 容器(挂 /tmp/.test-mode)
fault_gw(){
  docker rm -f "policy-gw-$2" 2>/dev/null
  docker run -d --name "policy-gw-$2" --network hiclab-net --restart no \
    -v /tmp/.test-mode:/tmp/.test_mode \
    -e ROLE_TOKENS="$ROLE_TOKENS_VAL" -e UPSTREAM_URL="http://github-mcp:8082/sse" \
    -e AUDIT_DSN="$AUDIT_DSN_VAL" -e L2_DSN="$L2_DSN_VAL" \
    -e L2_TIMEOUT_SECONDS=2 -e FAULT_INJECT="$1" \
    policy-gateway:latest >/dev/null 2>&1
  docker network connect mcp-backend-net "policy-gw-$2" 2>/dev/null
  for i in $(seq 1 15); do docker logs "policy-gw-$2" 2>&1 | grep -qa "upstream ready" && break; sleep 1; done
  docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py "policy-gw-$2":/tmp/probe-tools.py >/dev/null 2>&1
}

# ─── setup ───
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4bf-%'; DELETE FROM approvals WHERE run_id LIKE 'b4bf-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4bf-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4bf-%';" >/dev/null 2>&1
SU "INSERT INTO task_runs(run_id,status,repo,pr_number) VALUES('b4bf-run','SUBMITTED','nghqqa/MergePilot',99999);" >/dev/null 2>&1
BR="fix/b4bf-$$"
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_branch owner=nghqqa repo=MergePilot branch=$BR from_branch=main 2>&1 | grep -qi ref
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_or_update_file owner=nghqqa repo=MergePilot path=fault-$$.md branch=$BR content=test message=test 2>&1 | grep -qi commit
PR_RES=$(docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_pull_request owner=nghqqa repo=MergePilot head=$BR base=main title="B4b fault" body=auto 2>&1)
PR_NUM=$(echo "$PR_RES" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1)
if [ -z "$PR_NUM" ]; then log "PR creation failed; abort"; echo "PR_FAIL PASS=0 FAIL=1" >> "$OUT"; exit 1; fi
PR_INFO=$(docker exec policy-gw python3 /tmp/probe-tools.py reviewer --call pull_request_read method=get owner=nghqqa repo=MergePilot pullNumber=$PR_NUM 2>&1)
HEAD_SHA=$(echo "$PR_INFO" | grep -oE '[0-9a-f]{40}' | head -1)
SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4bf','b4bf-run','nghqqa/MergePilot',$PR_NUM,'$BR','main','$HEAD_SHA');" >/dev/null 2>&1
MPAYLOAD="{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":$PR_NUM,\"commit_title\":\"fault\",\"merge_method\":\"squash\"}"
MAH=$(chash "$MPAYLOAD")
log "PR=$PR_NUM head=${HEAD_SHA:0:12}"

log "═══════════════════════════════════════════════"
log "  B4b fault-path coverage"
log "═══════════════════════════════════════════════"

# ─── 1. bad audit DSN → AUDIT_UNAVAILABLE + PR 未变 ───
log ""; log "=== 1. bad audit DSN → AUDIT_UNAVAILABLE + PR 仍 open ==="
docker rm -f policy-gw-noaudit 2>/dev/null
docker run -d --name policy-gw-noaudit --network hiclab-net --restart no \
  -e ROLE_TOKENS="$ROLE_TOKENS_VAL" -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="postgresql://x:x@audit-pg-unreachable:5432/x" \
  -e L2_DSN="$L2_DSN_VAL" policy-gateway:latest >/dev/null 2>&1
docker network connect mcp-backend-net policy-gw-noaudit 2>/dev/null
for i in $(seq 1 15); do docker logs policy-gw-noaudit 2>&1 | grep -qa "upstream ready" && break; sleep 1; done
docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw-noaudit:/tmp/probe-tools.py >/dev/null 2>&1
ATKT=$(SU "SELECT l2_create_ticket('bnd-b4bf','merge','$MPAYLOAD'::jsonb,'$MAH',24,1);")
SU "SELECT l2_approve('$ATKT','b4bf@host');" >/dev/null 2>&1
AR=$(docker exec policy-gw-noaudit python3 /tmp/probe-tools.py coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$PR_NUM commit_title=fault merge_method=squash approval_ticket=$ATKT 2>&1 | head -3)
AST=$(SU "SELECT status FROM approvals WHERE ticket_id='$ATKT';")
echo "$AR" | grep -qi "AUDIT_UNAVAILABLE" && ok "bad audit DSN → AUDIT_UNAVAILABLE" || bad "expected AUDIT_UNAVAILABLE: $(echo "$AR"|tail -1)"
[ "$AST" = "FAILED" ] && ok "ticket=FAILED after audit fail" || bad "ticket=$AST (expected FAILED)"
# 断言 PR 仍 open + head SHA 不变(GitHub 未被调用)
PR_AFTER=$(docker exec policy-gw python3 /tmp/probe-tools.py reviewer --call pull_request_read method=get owner=nghqqa repo=MergePilot pullNumber=$PR_NUM 2>&1)
PR_STATE=$(echo "$PR_AFTER" | grep -oE '"state":"[^"]*"' | head -1)
PR_SHA=$(echo "$PR_AFTER" | grep -oE '[0-9a-f]{40}' | head -1)
echo "$PR_STATE" | grep -qi "open" && ok "PR still open (GitHub not called)" || bad "PR state changed: $PR_STATE"
[ "$PR_SHA" = "$HEAD_SHA" ] && ok "PR head SHA unchanged" || bad "head SHA changed: ${PR_SHA:0:12} != ${HEAD_SHA:0:12}"
docker stop policy-gw-noaudit >/dev/null 2>&1; docker rm policy-gw-noaudit >/dev/null 2>&1

# ─── 1b. close → USED(独立 PR2,不和 fault 测试共享 PR1)───
log ""; log "=== 1b. close → USED (independent PR2) ==="
CBR="fix/b4bf-close-$$"
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_branch owner=nghqqa repo=MergePilot branch=$CBR from_branch=main 2>&1 | grep -qi ref
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_or_update_file owner=nghqqa repo=MergePilot path=close-$$.md branch=$CBR content=close message=close 2>&1 | grep -qi commit
CPR_RES=$(docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_pull_request owner=nghqqa repo=MergePilot head=$CBR base=main title="B4b close" body=auto 2>&1)
CPR_NUM=$(echo "$CPR_RES" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1)
if [ -n "$CPR_NUM" ]; then
  CPR_INFO=$(docker exec policy-gw python3 /tmp/probe-tools.py reviewer --call pull_request_read method=get owner=nghqqa repo=MergePilot pullNumber=$CPR_NUM 2>&1)
  CHEAD_SHA=$(echo "$CPR_INFO" | grep -oE '[0-9a-f]{40}' | head -1)
  SU "INSERT INTO task_runs(run_id,status,repo,pr_number) VALUES('b4bf-close-run','SUBMITTED','nghqqa/MergePilot',$CPR_NUM);" >/dev/null 2>&1
  SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4bf-close','b4bf-close-run','nghqqa/MergePilot',$CPR_NUM,'$CBR','main','$CHEAD_SHA');" >/dev/null 2>&1
  CPAYLOAD="{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":$CPR_NUM,\"state\":\"closed\"}"
  CAH=$(chash "$CPAYLOAD")
  CTKT=$(SU "SELECT l2_create_ticket('bnd-b4bf-close','close','$CPAYLOAD'::jsonb,'$CAH',24,1);")
  SU "SELECT l2_approve('$CTKT','b4bf@host');" >/dev/null 2>&1
  CR=$(GW update_pull_request owner=nghqqa repo=MergePilot pullNumber=$CPR_NUM state=closed approval_ticket=$CTKT)
  CST=$(SU "SELECT status FROM approvals WHERE ticket_id='$CTKT';")
  log "  close PR=$CPR_NUM ticket=$CST"
  [ "$CST" = "USED" ] && ok "close → USED" || bad "close failed: $CST"
else
  bad "close PR creation failed"
fi

# ─── 2-5. fault inject(write_timeout LAST — 可能合并 PR1,不影响后续)───
for mode in toctou_timeout upstream_error complete_error write_timeout; do
  log ""; log "=== fault: $mode ==="
  FTKT=$(SU "SELECT l2_create_ticket('bnd-b4bf','merge','$MPAYLOAD'::jsonb,'$MAH',24,1);")
  SU "SELECT l2_approve('$FTKT','b4bf@host');" >/dev/null 2>&1
  fault_gw "$mode" "$mode"
  FR=$(docker exec "policy-gw-$mode" python3 /tmp/probe-tools.py coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$PR_NUM commit_title=fault merge_method=squash approval_ticket=$FTKT 2>&1 | head -3)
  FST=$(SU "SELECT status FROM approvals WHERE ticket_id='$FTKT';")
  FAUD=$(SU "SELECT reason_code FROM mcp_calls WHERE ticket_id='$FTKT' AND phase IN ('ERROR','RESULT') ORDER BY ts DESC LIMIT 1;")
  UP_CALLED=$(docker logs "policy-gw-$mode" 2>&1 | grep -c "L2 WRITE.*calling upstream" || true)
  log "  $mode: ticket=$FST audit=$FAUD upstream_called=$UP_CALLED"
  docker stop "policy-gw-$mode" >/dev/null 2>&1; docker rm "policy-gw-$mode" >/dev/null 2>&1
  case "$mode" in
    toctou_timeout) [ "$FST" = "FAILED" ] && ok "TOCTOU read timeout → FAILED (write not sent)" || bad "expected FAILED: $FST" ;;
    write_timeout)
      [ "$FST" = "UNKNOWN" ] && ok "write timeout → UNKNOWN (no retry)" || bad "expected UNKNOWN: $FST"
      [ "${UP_CALLED:-0}" -ge 1 ] && ok "upstream.call_tool entered before timeout" || bad "upstream never entered" ;;
    upstream_error) [ "$FST" = "FAILED" ] && ok "upstream is_error → FAILED" || bad "expected FAILED: $FST" ;;
    complete_error) [ "$FST" = "EXECUTING" ] && ok "complete DB_ERROR → EXECUTING (STATE_COMMIT_PENDING)" || bad "expected EXECUTING: $FST" ;;
  esac
done

# ─── 6. negative startup: FAULT_INJECT without /tmp/.test_mode → refuse to start ───
log ""; log "=== 7. FAULT_INJECT without .test_mode → refuse to start ==="
docker rm -f policy-gw-notest 2>/dev/null
docker run -d --name policy-gw-notest --network hiclab-net --restart no \
  -e ROLE_TOKENS="$ROLE_TOKENS_VAL" -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="$AUDIT_DSN_VAL" -e L2_DSN="$L2_DSN_VAL" \
  -e FAULT_INJECT=write_timeout \
  policy-gateway:latest >/dev/null 2>&1
sleep 3
NOTEST_LOGS=$(docker logs policy-gw-notest 2>&1)
echo "$NOTEST_LOGS" | grep -qi "refusing to start" && ok "FAULT_INJECT without .test_mode → refuse to start" || bad "gateway should refuse: $(echo "$NOTEST_LOGS"|head -1)"
docker stop policy-gw-notest >/dev/null 2>&1; docker rm policy-gw-notest >/dev/null 2>&1

# ─── 8. production assertion: main gateway has no FAULT_INJECT, no .test_mode ───
log ""; log "=== 8. production gateway clean (no FAULT_INJECT, no .test_mode) ==="
PROD_FI=$(docker exec policy-gw printenv FAULT_INJECT 2>/dev/null)
PROD_TM=$(docker exec policy-gw test -f /tmp/.test_mode && echo yes 2>/dev/null)
[ -z "$PROD_FI" ] && ok "production gateway: FAULT_INJECT not set" || bad "production has FAULT_INJECT=$PROD_FI"
[ -z "$PROD_TM" ] && ok "production gateway: /tmp/.test_mode absent" || bad "production has .test_mode"

# ─── evidence snapshot (not counted as PASS) ───
log ""; log "=== evidence snapshot ==="
mkdir -p /mnt/d/goai/evidence/m3b-b4b
SU "SELECT ticket_id,action,status,execution_id,result_sha,error FROM approvals WHERE run_id LIKE 'b4bf-%' ORDER BY created_at;" > /mnt/d/goai/evidence/m3b-b4b/fault-approvals.txt 2>/dev/null
# Fix: audit-summary query (join properly; not empty)
SU "SELECT m.ticket_id,m.phase,m.decision,m.reason_code,a.action,a.status FROM mcp_calls m JOIN approvals a ON m.ticket_id=a.ticket_id WHERE a.run_id LIKE 'b4bf-%' ORDER BY m.ticket_id,m.ts;" > /mnt/d/goai/evidence/m3b-b4b/fault-audit.txt 2>/dev/null
AUD_LINES=$(wc -l < /mnt/d/goai/evidence/m3b-b4b/fault-audit.txt 2>/dev/null || echo 0)
log "  approvals + audit ($AUD_LINES lines) written to evidence/m3b-b4b/"
# assert audit file non-empty (not counted as functional PASS, but logged)
[ "${AUD_LINES:-0}" -gt 0 ] && log "  audit evidence non-empty ($AUD_LINES rows)" || { log "  !!! audit evidence EMPTY"; FAIL=$((FAIL+1)); }

# cleanup DB rows (evidence already snapshot)
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4bf-%'; DELETE FROM approvals WHERE run_id LIKE 'b4bf-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4bf-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4bf-%';" >/dev/null 2>&1

log ""
log "═══════════════════════════════════════════════"
log "  B4b fault-path: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ] || exit 1
