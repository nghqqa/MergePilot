#!/bin/bash
# m3b-b4a-test.sh — B4a 数据库与权限验收。
# 全生命周期:绑定→建票(PENDING)→审批(APPROVED)→claim 错误 hash 拒(保持 APPROVED)
# →claim 正确(EXECUTING,返回 canonical_payload + execution_id)→complete(USED)。
# 验证:account EXECUTE-only、task_runs APPROVAL_PENDING、SECURITY DEFINER 属性、args_hash 完整 64hex。
# 退出码:全过 0,否则 1。
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b4a-test.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

DIR=/home/ngh/.config/mergepilot
CTRL="$DIR/controller.env"; B4ENV="$DIR/b4-roles.env"
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_DB=${PG_DB:-mergepilot_audit}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
L2_PW=$(grep '^POLICY_GATEWAY_L2_PASS=' "$B4ENV" | head -1 | cut -d= -f2-)
APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' "$B4ENV" | head -1 | cut -d= -f2-)

# 统一 psql 调用:user pw -c sql → 输出(2>&1 保留 permission denied 等错误信息)
psql_as(){ local u="$1" pw="$2" sql="$3"; docker exec -e PGPASSWORD="$pw" audit-pg psql -U "$u" -d "$PG_DB" -t -A -c "$sql" 2>&1; }
SU(){ psql_as "$PG_SU" "$SU_PW" "$1"; }
L2(){ psql_as policy_gateway_l2 "$L2_PW" "$1"; }
APV(){ psql_as mergepilot_approver "$APV_PW" "$1"; }

log "═══════════════════════════════════════════════"
log "  B4a 验收(schema + 函数 + EXECUTE-only 账号)"
log "═══════════════════════════════════════════════"

# ─── 1. schema 迁移确认 ───
log ""; log "=== 1. schema 迁移 ==="
C1=$(SU "SELECT count(*) FROM information_schema.columns WHERE table_name='approvals' AND column_name IN ('binding_id','attempt_no','canonical_payload','args_hash','execution_id','executing_at','approval_expires_at','exec_ttl_hours');")
[ "${C1:-0}" = "8" ] && ok "approvals v2 8 列齐全" || bad "approvals v2 列不足($C1/8)"
C2=$(SU "SELECT count(*) FROM information_schema.columns WHERE table_name='run_pr_bindings';")
[ "${C2:-0}" = "8" ] && ok "run_pr_bindings 8 列" || bad "run_pr_bindings 列异常($C2)"
C3=$(SU "SELECT count(*) FROM information_schema.columns WHERE table_name='policy_action_outbox' AND column_name='lease_expires_at';")
[ "${C3:-0}" = "1" ] && ok "outbox lease_expires_at 在" || bad "outbox 无 lease"
C4=$(SU "SELECT is_nullable FROM information_schema.columns WHERE table_name='approvals' AND column_name='expires_at';")
[ "$C4" = "YES" ] && ok "approvals.expires_at 可 NULL(PENDING 阶段)" || bad "expires_at 仍 NOT NULL"
C5=$(SU "SELECT COUNT(*) FROM pg_constraint WHERE conname='chk_task_status' AND pg_get_constraintdef(oid) LIKE '%APPROVAL_PENDING%';")
[ "${C5:-0}" = "1" ] && ok "task_runs CHECK 含 APPROVAL_PENDING" || bad "task_runs 无 APPROVAL_PENDING"

# ─── 2. 函数 SECURITY DEFINER + 固定 search_path ───
log ""; log "=== 2. 函数硬化属性 ==="
for fn in l2_create_ticket l2_claim_ticket l2_complete_ticket l2_approve l2_pending_list l2_reconcile_unknown; do
  ATTR=$(SU "SELECT (prosecdef AND proconfig::text LIKE '%search_path=pg_catalog%') FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid WHERE p.proname='$fn' AND n.nspname='public' LIMIT 1;")
  [ "$ATTR" = "t" ] && ok "$fn SECURITY DEFINER + search_path=pg_catalog" || bad "$fn 属性异常: '$ATTR'"
done

# ─── 3. 全生命周期 ───
log ""; log "=== 3. 全生命周期(建票→审批→claim→complete)==="
# 清理上次残留 + 建测试 task_run(task_runs 无 task_id 列;用 run_id/status/repo/pr_number)+ binding
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4atest-%'; DELETE FROM approvals WHERE run_id LIKE 'b4atest-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4atest-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4atest-%';" >/dev/null 2>&1
SU "INSERT INTO task_runs(run_id,status,repo,pr_number) VALUES('b4atest-run','SUBMITTED','nghqqa/MergePilot',99999);" >/dev/null 2>&1
SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4atest','b4atest-run','nghqqa/MergePilot',99999,'fix/b4atest-1','main','deadbeefcafebabe000000000000000000000000');" >/dev/null 2>&1

# canonical_payload + args_hash(完整 64hex,固定 canonical:sort_keys + 紧凑分隔)
PAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":99999,"commit_title":"merge fix","merge_method":"squash"}'
ARGS_HASH=$(python3 -c "import hashlib,json,sys; print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]),sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$PAYLOAD")
log "  args_hash=$ARGS_HASH (len=${#ARGS_HASH})"
[ "${#ARGS_HASH}" = "64" ] && ok "args_hash 完整 64hex" || bad "args_hash 长度异常(${#ARGS_HASH})"

# 3a. 建票(Controller)
TKT=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);")
log "  ticket=$TKT"
[ -n "$TKT" ] && echo "$TKT" | grep -q "^tkt-" && ok "l2_create_ticket 建票($TKT)" || bad "建票失败: $TKT"
STATUS=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';")
[ "$STATUS" = "PENDING" ] && ok "票据 PENDING" || bad "状态异常: $STATUS"
ATT=$(SU "SELECT attempt_no FROM approvals WHERE ticket_id='$TKT';")
[ "$ATT" = "1" ] && ok "attempt_no=1" || bad "attempt 异常: $ATT"
EXE=$(SU "SELECT expires_at FROM approvals WHERE ticket_id='$TKT';")
[ "$EXE" = "" ] && ok "PENDING 阶段 expires_at=NULL" || bad "PENDING expires_at 非 NULL: $EXE"
OBX=$(SU "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT';")
[ "$OBX" = "PENDING_DISPATCH" ] && ok "outbox 同事务写 PENDING_DISPATCH" || bad "outbox 异常: $OBX"

# 3b. 审批(approver)
APR=$(APV "SELECT l2_approve('$TKT','tester@host');")
[ "$APR" = "t" ] && ok "l2_approve(approver 账号)→ APPROVED" || bad "approve 失败: $APR"
STATUS=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';"); [ "$STATUS" = "APPROVED" ] && ok "APPROVED" || bad "状态: $STATUS"
APVBY=$(SU "SELECT approved_by FROM approvals WHERE ticket_id='$TKT';"); [ "$APVBY" = "tester@host" ] && ok "approved_by=tester@host" || bad "approved_by: $APVBY"
EXE2=$(SU "SELECT expires_at > now() FROM approvals WHERE ticket_id='$TKT';"); [ "$EXE2" = "t" ] && ok "APPROVED 后 expires_at 已写(+1h)" || bad "expires_at 未写"

# 3c. claim 错误 args_hash → 拒,票据保持 APPROVED
WRONG=$(python3 -c "print('a'*64)")
CL0=$(L2 "SELECT count(*) FROM l2_claim_ticket('$TKT','merge','nghqqa/MergePilot',99999,'$WRONG');")
log "  claim 错 hash 行数=$CL0"
[ "$CL0" = "0" ] && ok "错误 args_hash → claim 返回 0 行(票据未消耗)" || bad "错 hash 不该消耗票据"
ST0=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';"); [ "$ST0" = "APPROVED" ] && ok "票据仍 APPROVED(CAS 未匹配不消耗)" || bad "票据被错误消耗: $ST0"

# 3d. claim 正确 → EXECUTING + canonical_payload + execution_id
CL1=$(L2 "SELECT execution_id || '|' || canonical_payload::text FROM l2_claim_ticket('$TKT','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');")
EID="${CL1%%|*}"; PAY="${CL1#*|}"
log "  execution_id=$EID  payload=$PAY"
[ -n "$EID" ] && echo "$EID" | grep -qE "^[0-9a-f-]{36}$" && ok "claim 返回 execution_id" || bad "claim 未返回 execution_id: $CL1"
echo "$PAY" | grep -qi "merge_method.*squash" && ok "claim 返回 canonical_payload(含 merge_method=squash)" || bad "payload 异常: $PAY"
ST1=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';"); [ "$ST1" = "EXECUTING" ] && ok "票据 EXECUTING" || bad "状态: $ST1"

# 3e. 并发/重复 claim 同票 → 第二次 0 行(已 EXECUTING)
CL2=$(L2 "SELECT count(*) FROM l2_claim_ticket('$TKT','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');")
[ "$CL2" = "0" ] && ok "重复 claim → 0 行(防并发双执行)" || bad "重复 claim 不该再消耗"

# 3f. complete → USED(用 claim 返回的 execution_id)
COMP=$(L2 "SELECT l2_complete_ticket('$TKT','$EID'::uuid,'mergesha123');")
[ "$COMP" = "t" ] && ok "l2_complete_ticket → USED" || bad "complete 失败: $COMP"
ST2=$(SU "SELECT status||'|'||result_sha FROM approvals WHERE ticket_id='$TKT';")
echo "$ST2" | grep -q "USED|mergesha123" && ok "USED + result_sha=mergesha123" || bad "complete 状态: $ST2"

# 3g. 错误 execution_id 的 complete → 拒(防伪造)
COMP2=$(L2 "SELECT l2_complete_ticket('$TKT',gen_random_uuid()::uuid,'evil');")
[ "$COMP2" = "f" ] && ok "错误 execution_id 的 complete 被拒" || bad "execution_id 校验失效"

# ─── 4. task_runs APPROVAL_PENDING 可用 ───
log ""; log "=== 4. task_runs APPROVAL_PENDING ==="
TSK=$(SU "UPDATE task_runs SET status='APPROVAL_PENDING' WHERE run_id='b4atest-run' RETURNING status;" 2>&1)
echo "$TSK" | grep -q "APPROVAL_PENDING" && ok "task_runs 可转 APPROVAL_PENDING" || bad "task_runs APPROVAL_PENDING 被拒: $TSK"

# ─── 5. 账号 EXECUTE-only(再确认)───
log ""; log "=== 5. 账号隔离 ==="
TBL=$(L2 "SELECT count(*) FROM approvals;" 2>&1)
echo "$TBL" | grep -qi "permission denied" && ok "gateway_l2 不能 SELECT approvals" || bad "gateway_l2 不该有 SELECT: $TBL"
TBL2=$(APV "SELECT count(*) FROM policy_action_outbox;" 2>&1)
echo "$TBL2" | grep -qi "permission denied" && ok "approver 不能读 outbox" || bad "approver 不该读 outbox: $TBL2"

# 清理
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4atest-%'; DELETE FROM approvals WHERE run_id LIKE 'b4atest-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4atest-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4atest-%';" >/dev/null 2>&1

log ""
log "═══════════════════════════════════════════════"
log "  B4a 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ] || exit 1
