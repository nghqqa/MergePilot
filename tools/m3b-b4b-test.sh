#!/bin/bash
# m3b-b4b-test.sh — B4b Gateway 边界验收(L2 claim→TOCTOU→上游→complete/fail/mark_unknown)。
# 覆盖:缺票/伪造票/hash不匹配/过期/TOCTOU/成功merge/close/并发/审计。
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b4b-test.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }
DIR=/home/ngh/.config/mergepilot
CTRL="$DIR/controller.env"
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=${PG_DB:-mergepilot_audit}; PG_SU=${PG_SU:-mergepilot}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
SU(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>&1; }
GW(){ docker exec policy-gw python3 /tmp/probe-tools.py coordinator --call "${@}" 2>&1 | head -5; }
deny_is(){ echo "$1" | grep -qiE "POLICY_DENIED.*$2"; }

# canonical args_hash helper(Python,与 gateway 一致:排除 approval_ticket,sort_keys+紧凑)
chash(){ python3 -c "import hashlib,json,sys; d=json.loads(sys.argv[1]); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }

log "═══════════════════════════════════════════════"
log "  B4b Gateway 边界验收"
log "═══════════════════════════════════════════════"
docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py >/dev/null 2>&1

# ─── setup:清理 + 建 task_run + binding(用 PR 99999 = 不存在的 PR,测 TOCTOU 失败路径)───
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4btest-%'; DELETE FROM approvals WHERE run_id LIKE 'b4btest-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4btest-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4btest-%';" >/dev/null 2>&1
SU "INSERT INTO task_runs(run_id,status,repo,pr_number) VALUES('b4btest-run','SUBMITTED','nghqqa/MergePilot',99999);" >/dev/null 2>&1
SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4b','b4btest-run','nghqqa/MergePilot',99999,'fix/b4b-test','main','fakehead0000000000000000000000000000000000');" >/dev/null 2>&1

# ─── 1. 缺票 → L2_TICKET_REQUIRED ───
log ""; log "=== 1. coordinator merge 缺 approval_ticket → L2_TICKET_REQUIRED ==="
R1=$(GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=99999 commit_title=test merge_method=squash)
deny_is "$R1" "L2_TICKET_REQUIRED" && ok "缺票 → L2_TICKET_REQUIRED" || bad "缺票应拒"

# ─── 2. 伪造票 → CLAIM_MISMATCH ───
log ""; log "=== 2. 伪造票 → CLAIM_MISMATCH ==="
PAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":99999,"commit_title":"test","merge_method":"squash"}'
AH=$(chash "$PAYLOAD")
R2=$(GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=99999 commit_title=test merge_method=squash approval_ticket=tkt-nonexistent-00000000)
deny_is "$R2" "CLAIM_MISMATCH" && ok "伪造票 → CLAIM_MISMATCH(不调 GitHub)" || bad "伪造票应拒: $(echo "$R2"|tail -1)"

# ─── 3. 合法票但 PR 不存在 → claim 成功 → TOCTOU_MISMATCH ───
log ""; log "=== 3. 合法票 claim → TOCTOU(PR 不存在)→ FAILED ==="
TKT=$(SU "SELECT l2_create_ticket('bnd-b4b','merge','$PAYLOAD'::jsonb,'$AH',24,1);")
SU "SELECT l2_approve('$TKT','b4btest@host');" >/dev/null 2>&1
log "  ticket=$TKT status=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';")"
R3=$(GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=99999 commit_title=test merge_method=squash approval_ticket=$TKT)
deny_is "$R3" "TOCTOU_MISMATCH" && ok "PR 不存在 → TOCTOU_MISMATCH + 票 FAILED" || bad "应 TOCTOU 拒: $(echo "$R3"|tail -1)"
ST3=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';")
[ "$ST3" = "FAILED" ] && ok "TOCTOU 失败后票=FAILED" || bad "票状态异常: $ST3"

# ─── 4. args_hash 不匹配(call 传不同 commit_title)→ CLAIM_MISMATCH ───
log ""; log "=== 4. call 与票 args_hash 不匹配 → CLAIM_MISMATCH(票不消耗) ==="
TKT4=$(SU "SELECT l2_create_ticket('bnd-b4b','merge','$PAYLOAD'::jsonb,'$AH',24,1);")
SU "SELECT l2_approve('$TKT4','b4btest@host');" >/dev/null 2>&1
R4=$(GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=99999 commit_title=DIFFERENT merge_method=squash approval_ticket=$TKT4)
deny_is "$R4" "CLAIM_MISMATCH" && ok "commit_title 不同 → hash 不匹配 → CLAIM_MISMATCH" || bad "hash 应拒"
ST4=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT4';")
[ "$ST4" = "APPROVED" ] && ok "hash 不匹配时票保持 APPROVED(未消耗)" || bad "票被消耗: $ST4"

# ─── 5. 过期票 → CLAIM_MISMATCH ───
log ""; log "=== 5. 过期票(执行期已过)→ CLAIM_MISMATCH ==="
TKT5=$(SU "SELECT l2_create_ticket('bnd-b4b','merge','$PAYLOAD'::jsonb,'$AH',24,1);")
SU "SELECT l2_approve('$TKT5','b4btest@host');" >/dev/null 2>&1
SU "UPDATE approvals SET expires_at = now() - interval '1 hour' WHERE ticket_id='$TKT5';" >/dev/null 2>&1
R5=$(GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=99999 commit_title=test merge_method=squash approval_ticket=$TKT5)
deny_is "$R5" "CLAIM_MISMATCH" && ok "过期票 → CLAIM_MISMATCH(expires_at 检查)" || bad "过期应拒"

# ─── 6. 审计验证(INTENT 带 ticket_id + execution_id)───
log ""; log "=== 6. 审计 INTENT 带 ticket_id+execution_id ==="
AUD=$(SU "SELECT count(*) FROM mcp_calls WHERE ticket_id='$TKT' AND phase='ERROR' AND reason_code='TOCTOU_MISMATCH';")
[ "${AUD:-0}" != "0" ] && ok "TOCTOU DENY 审计有 ticket_id(ERROR phase)" || bad "缺 TOCTOU 审计"
EID_AUD=$(SU "SELECT count(*) FROM mcp_calls WHERE ticket_id IS NOT NULL AND execution_id IS NOT NULL AND phase IN ('INTENT','RESULT','ERROR');")
[ "${EID_AUD:-0}" != "0" ] && ok "审计行带 ticket_id + execution_id(B4b)" || bad "审计缺 ticket_id/execution_id"

# ─── 7. 成功 merge(真实 PR:fixer 建分支 + PR → coordinator claim+merge)───
log ""; log "=== 7. 成功 merge(真实 fix 分支 + PR → USED) ==="
BR="fix/b4b-merge-$$"
# fixer 建分支 + 加一个 commit(否则 PR diff 空 → GitHub 422)
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_branch owner=nghqqa repo=MergePilot branch=$BR from_branch=main 2>&1 | grep -qi "ref" && log "  分支 $BR 已建" || log "  分支可能已存在(继续)"
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_or_update_file owner=nghqqa repo=MergePilot path=b4b-test-$$.md branch=$BR content="b4b merge test" message="b4b test commit" 2>&1 | grep -qi "commit\|content\|sha" && log "  commit 已加" || log "  commit 可能已存在(继续)"
# fixer 建 PR
PR_RES=$(docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_pull_request owner=nghqqa repo=MergePilot head=$BR base=main title="B4b merge test" body="auto" 2>&1)
PR_NUM=$(echo "$PR_RES" | grep -oE 'pull/[0-9]+|"number"[^0-9]*[0-9]+' | grep -oE '[0-9]+' | head -1)
# 读 PR 拿权威 head_sha(GitHub MCP 返回可能缺 number/sha,经 pull_request_read 更可靠)
if [ -n "$PR_NUM" ]; then
  PR_INFO=$(docker exec policy-gw python3 /tmp/probe-tools.py reviewer --call pull_request_read method=get owner=nghqqa repo=MergePilot pullNumber=$PR_NUM 2>&1)
  HEAD_SHA=$(echo "$PR_INFO" | grep -oE '[0-9a-f]{40}' | head -1)
fi
log "  PR number=$PR_NUM head_sha=${HEAD_SHA:0:12}..."
if [ -z "$PR_NUM" ]; then bad "PR 创建失败: $(echo "$PR_RES"|head -1)"; else
  # 更新 binding 为真实 PR
  SU "UPDATE run_pr_bindings SET pr_number=$PR_NUM, head_sha='$HEAD_SHA', fix_branch='$BR' WHERE binding_id='bnd-b4b';" >/dev/null 2>&1
  # 创建 merge 票
  MPAYLOAD="{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":$PR_NUM,\"commit_title\":\"B4b merge\",\"merge_method\":\"squash\"}"
  MAH=$(chash "$MPAYLOAD")
  MTKT=$(SU "SELECT l2_create_ticket('bnd-b4b','merge','$MPAYLOAD'::jsonb,'$MAH',24,1);")
  SU "SELECT l2_approve('$MTKT','b4btest@host');" >/dev/null 2>&1
  # coordinator 经 gateway merge
  R7=$(GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$PR_NUM commit_title="B4b merge" merge_method=squash approval_ticket=$MTKT)
  ST7=$(SU "SELECT status FROM approvals WHERE ticket_id='$MTKT';")
  log "  merge 结果: $(echo "$R7"|tail -1|head -c 80)  票=$ST7"
  [ "$ST7" = "USED" ] && ok "成功 merge → USED" || bad "merge 失败: $ST7 ($(echo "$R7"|tail -1|head -c 60))"
  # 验审计 RESULT
  R7_AUD=$(SU "SELECT count(*) FROM mcp_calls WHERE ticket_id='$MTKT' AND phase='RESULT' AND decision='ALLOW' AND reason_code='L2_COMPLETE';")
  [ "${R7_AUD:-0}" != "0" ] && ok "merge 成功审计 RESULT(L2_COMPLETE)" || bad "缺 RESULT 审计"
fi


# --- 8. true concurrent mutual exclusion (new PR + same ticket + 2 parallel) ---
log ""; log "=== 8. true concurrent (same PR + same ticket + 2 parallel) ==="
CBR="fix/b4b-conc-$$"
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_branch owner=nghqqa repo=MergePilot branch=$CBR from_branch=main 2>&1 | grep -qi ref && log "  branch $CBR created"
docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_or_update_file owner=nghqqa repo=MergePilot path=conc-$$.md branch=$CBR content=conc message=conc 2>&1 | grep -qi commit && log "  commit added"
CPR_RES=$(docker exec policy-gw python3 /tmp/probe-tools.py fixer --call create_pull_request owner=nghqqa repo=MergePilot head=$CBR base=main title="B4b concurrent" body=auto 2>&1)
CPR_NUM=$(echo "$CPR_RES" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1)
if [ -n "$CPR_NUM" ]; then
  CPR_INFO=$(docker exec policy-gw python3 /tmp/probe-tools.py reviewer --call pull_request_read method=get owner=nghqqa repo=MergePilot pullNumber=$CPR_NUM 2>&1)
  CHEAD_SHA=$(echo "$CPR_INFO" | grep -oE '[0-9a-f]{40}' | head -1)
  SU "UPDATE run_pr_bindings SET pr_number=$CPR_NUM, head_sha='$CHEAD_SHA', fix_branch='$CBR' WHERE binding_id='bnd-b4b';" >/dev/null 2>&1
  CCPAYLOAD="{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":$CPR_NUM,\"commit_title\":\"conc\",\"merge_method\":\"squash\"}"
  CCAH=$(chash "$CCPAYLOAD")
  CCTKT=$(SU "SELECT l2_create_ticket('bnd-b4b','merge','$CCPAYLOAD'::jsonb,'$CCAH',24,1);")
  SU "SELECT l2_approve('$CCTKT','b4btest@host');" >/dev/null 2>&1
  GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$CPR_NUM commit_title=conc merge_method=squash approval_ticket=$CCTKT > /tmp/conc1.out 2>&1 &
  GW merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$CPR_NUM commit_title=conc merge_method=squash approval_ticket=$CCTKT > /tmp/conc2.out 2>&1 &
  wait
  CLAIMED=$(SU "SELECT count(*) FROM mcp_calls WHERE ticket_id='$CCTKT' AND reason_code='L2_CLAIMED';")
  CCTKT_ST=$(SU "SELECT status FROM approvals WHERE ticket_id='$CCTKT';")
  log "  claim_count=$CLAIMED ticket_status=$CCTKT_ST"
  [ "$CLAIMED" = "1" ] && ok "concurrent: only 1 claim (CAS mutual exclusion)" || bad "concurrent claim count=$CLAIMED"
  [ "$CCTKT_ST" = "USED" ] && ok "concurrent: winner -> USED" || bad "ticket status=$CCTKT_ST"
else
  bad "concurrent PR creation failed"; CPR_NUM=""
fi

# --- 9. bad L2 DSN -> L2_DB_UNAVAILABLE ---
log ""; log "=== 9. bad L2 DSN -> L2_DB_UNAVAILABLE ==="
source /home/ngh/.config/mergepilot/audit-db.env 2>/dev/null; AUDIT_DSN_REF="postgresql://${PGW_AUDIT_USER}:${PGW_AUDIT_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
ROLE_TOKENS_REF=$(cat /home/ngh/.config/mergepilot/role-tokens.json)
docker rm -f policy-gw-nol2 2>/dev/null
docker run -d --name policy-gw-nol2 --network hiclab-net --restart no \
  -e ROLE_TOKENS="$ROLE_TOKENS_REF" -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="$AUDIT_DSN_REF" \
  -e L2_DSN="postgresql://policy_gateway_l2:wrong@audit-pg-unreachable:5432/mergepilot_audit" \
  policy-gateway:latest >/dev/null 2>&1
docker network connect mcp-backend-net policy-gw-nol2 2>/dev/null
for i in $(seq 1 15); do docker logs policy-gw-nol2 2>&1 | grep -qa "upstream ready" && break; sleep 1; done
docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw-nol2:/tmp/probe-tools.py >/dev/null 2>&1
if [ -n "$CPR_NUM" ]; then
  DPAYLOAD="{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":$CPR_NUM,\"commit_title\":\"conc\",\"merge_method\":\"squash\"}"
  DAH=$(chash "$DPAYLOAD")
  DTKT=$(SU "SELECT l2_create_ticket('bnd-b4b','merge','$DPAYLOAD'::jsonb,'$DAH',24,1);")
  SU "SELECT l2_approve('$DTKT','b4btest@host');" >/dev/null 2>&1
  R9=$(docker exec policy-gw-nol2 python3 /tmp/probe-tools.py coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$CPR_NUM commit_title=conc merge_method=squash approval_ticket=$DTKT 2>&1 | head -3)
  echo "$R9" | grep -qi "L2_DB_UNAVAILABLE" && ok "bad L2 DSN -> L2_DB_UNAVAILABLE (no GitHub call)" || bad "expected L2_DB_UNAVAILABLE: $(echo "$R9"|tail -1)"
  DST=$(SU "SELECT status FROM approvals WHERE ticket_id='$DTKT';")
  [ "$DST" = "APPROVED" ] && ok "ticket stays APPROVED when L2 DB unavailable" || bad "ticket status=$DST"
else
  bad "skipped bad-L2-DSN (no PR)"
fi
docker stop policy-gw-nol2 >/dev/null 2>&1; docker rm policy-gw-nol2 >/dev/null 2>&1

# --- evidence snapshot (before cleanup; not counted as PASS) ---
log ""; log "=== evidence snapshot ==="
mkdir -p /mnt/d/goai/evidence/m3b-b4b
SU "SELECT ticket_id,status,execution_id,result_sha FROM approvals WHERE run_id LIKE 'b4btest-%' ORDER BY created_at;" > /mnt/d/goai/evidence/m3b-b4b/approvals-snapshot.txt 2>/dev/null
SU "SELECT m.ticket_id,m.phase,m.decision,m.reason_code,a.action,a.status FROM mcp_calls m JOIN approvals a ON m.ticket_id=a.ticket_id WHERE a.run_id LIKE 'b4btest-%' ORDER BY m.ticket_id,m.ts;" > /mnt/d/goai/evidence/m3b-b4b/audit-summary.txt 2>/dev/null
AUD_LINES=$(wc -l < /mnt/d/goai/evidence/m3b-b4b/audit-summary.txt 2>/dev/null || echo 0)
log "  snapshot written (audit=$AUD_LINES rows)"
[ "${AUD_LINES:-0}" -gt 0 ] && log "  audit evidence non-empty" || { log "  !!! audit evidence EMPTY"; FAIL=$((FAIL+1)); }

# --- cleanup ---

# 清理(保留审计行;mcp_calls INSERT-only 不删)
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4btest-%'; DELETE FROM approvals WHERE run_id LIKE 'b4btest-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4btest-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4btest-%';" >/dev/null 2>&1
# 清理测试分支(GitHub 上)
[ -n "$BR" ] && docker exec policy-gw python3 /tmp/probe-tools.py coordinator --call delete_file owner=nghqqa repo=MergePilot path=.keep branch=$BR 2>/dev/null || true  # may fail; branches cleaned separately

log ""
log "═══════════════════════════════════════════════"
log "  B4b 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ] || exit 1
