#!/bin/bash
# m3b-b4a-test.sh — B4a + B4a.1 验收(DB schema + 函数 + EXECUTE-only 账号 + 漂移收敛)。
# 覆盖:全函数 owner=mergepilot_l2_owner、payload/binding 一致性、pending_list 全 payload、
#       完整状态机(create/approve/claim/complete/fail/mark_unknown/reconcile/expire)、
#       真并发 claim、漂移收敛、角色属性/成员、reconcile_executing 120s 约束。
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
psql_as(){ docker exec -e PGPASSWORD="$2" audit-pg psql -U "$1" -d "$PG_DB" -t -A -c "$3" 2>&1; }
SU(){ psql_as "$PG_SU" "$SU_PW" "$1"; }
L2(){ psql_as policy_gateway_l2 "$L2_PW" "$1"; }
APV(){ psql_as mergepilot_approver "$APV_PW" "$1"; }
L2FNS="l2_create_ticket,l2_claim_ticket,l2_complete_ticket,l2_fail_ticket,l2_mark_unknown,l2_approve,l2_pending_list,l2_reconcile_unknown,l2_reconcile_executing,l2_expire_pending"

log "═══════════════════════════════════════════════"
log "  B4a + B4a.1 验收"
log "═══════════════════════════════════════════════"

# ─── 1. schema 迁移 ───
log ""; log "=== 1. schema ==="
[ "$(SU "SELECT count(*) FROM information_schema.columns WHERE table_name='approvals' AND column_name IN ('binding_id','attempt_no','canonical_payload','args_hash','execution_id','executing_at','approval_expires_at','exec_ttl_hours');")" = "8" ] && ok "approvals v2 8 列" || bad "approvals v2 列不足"
[ "$(SU "SELECT count(*) FROM information_schema.columns WHERE table_name='run_pr_bindings';")" = "8" ] && ok "run_pr_bindings 8 列" || bad "run_pr_bindings 列数异常"
[ "$(SU "SELECT count(*) FROM information_schema.columns WHERE table_name='policy_action_outbox' AND column_name='lease_expires_at';")" = "1" ] && ok "outbox lease_expires_at" || bad "outbox 无 lease"
[ "$(SU "SELECT is_nullable FROM information_schema.columns WHERE table_name='approvals' AND column_name='expires_at';")" = "YES" ] && ok "expires_at 可 NULL" || bad "expires_at NOT NULL"
[ "$(SU "SELECT count(*) FROM pg_constraint WHERE conname='chk_task_status' AND pg_get_constraintdef(oid) LIKE '%APPROVAL_PENDING%';")" = "1" ] && ok "task_runs APPROVAL_PENDING" || bad "无 APPROVAL_PENDING"

# ─── 2. 全函数 owner=mergepilot_l2_owner + SECDEF + search_path(B4a.1 P1#1 + P2#5)───
log ""; log "=== 2. 全 l2_* 函数 owner + SECURITY DEFINER + search_path ==="
OWNER_OK=$(SU "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid JOIN pg_roles r ON p.proowner=r.oid WHERE p.proname IN ($(echo "$L2FNS" | tr ',' '\n' | sed "s/.*/'&'/" | paste -sd,)) AND n.nspname='public' AND r.rolname='mergepilot_l2_owner';")
log "  owner=mergepilot_l2_owner 的函数数: $OWNER_OK / 10"
[ "$OWNER_OK" = "10" ] && ok "10 个 l2_* 函数 owner 全是 mergepilot_l2_owner(NOLOGIN)" || bad "owner 异常($OWNER_OK/10)"
SECDEF_OK=$(SU "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid WHERE p.proname IN ($(echo "$L2FNS" | tr ',' '\n' | sed "s/.*/'&'/" | paste -sd,)) AND n.nspname='public' AND prosecdef AND proconfig::text LIKE '%search_path=pg_catalog%';")
[ "$SECDEF_OK" = "10" ] && ok "10 个函数全 SECURITY DEFINER + search_path=pg_catalog" || bad "SECDEF 异常($SECDEF_OK/10)"
PUB_OK=$(SU "SELECT count(*) FROM information_schema.role_routine_grants WHERE grantee='PUBLIC' AND routine_name IN ($(echo "$L2FNS" | tr ',' '\n' | sed "s/.*/'&'/" | paste -sd,));")
[ "${PUB_OK:-0}" = "0" ] && ok "l2_* 函数无 PUBLIC EXECUTE" || bad "PUBLIC 仍有 EXECUTE($PUB_OK)"

# ─── 3. 角色属性(无高危属性;membership 在 4z 用右方向检查,这里不重复旧方向)───
log ""; log "=== 3. 角色属性 ==="
ATTR_BAD=$(SU "SELECT count(*) FROM pg_roles WHERE rolname IN ('policy_gateway_l2','mergepilot_approver') AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication OR rolinherit);")
[ "${ATTR_BAD:-0}" = "0" ] && ok "两账号无 SUPERUSER/BYPASSRLS/CREATEDB/CREATEROLE/REPLICATION/INHERIT" || bad "高危属性残留($ATTR_BAD)"

# ─── 4. 全生命周期(含 fail/mark_unknown/reconcile/expire)───
log ""; log "=== 4. 全状态机 ==="
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4atest-%'; DELETE FROM approvals WHERE run_id LIKE 'b4atest-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4atest-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4atest-%';" >/dev/null 2>&1
SU "INSERT INTO task_runs(run_id,status,repo,pr_number) VALUES('b4atest-run','SUBMITTED','nghqqa/MergePilot',99999);" >/dev/null 2>&1
SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4atest','b4atest-run','nghqqa/MergePilot',99999,'fix/b4atest-1','main','deadbeef00000000000000000000000000000000');" >/dev/null 2>&1
PAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":99999,"commit_title":"merge fix","merge_method":"squash"}'
ARGS_HASH=$(python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]),sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$PAYLOAD")
[ "${#ARGS_HASH}" = "64" ] && ok "args_hash 完整 64hex" || bad "args_hash 长度(${#ARGS_HASH})"

# 4a. payload 与 binding 不一致 → 拒(B4a.1 P1#2)
BAD_PAYLOAD='{"owner":"evil","repo":"other","pullNumber":1,"commit_title":"x","merge_method":"squash"}'
BAD_RES=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$BAD_PAYLOAD'::jsonb,'$(python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]),sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$BAD_PAYLOAD")',24,1);" 2>&1)
echo "$BAD_RES" | grep -qi "binding repo" && ok "payload repo 与 binding 不一致 → 拒" || { echo "$BAD_RES" | grep -qi "pullNumber" && ok "payload pullNumber 与 binding 不一致 → 拒" || bad "payload/binding 一致性校验失效: $(echo "$BAD_RES"|head -1)"; }

# 4b. 正常建票
TKT=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);")
echo "$TKT" | grep -q "^tkt-" && ok "建票($TKT,PENDING)" || bad "建票失败: $TKT"
[ "$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';")" = "PENDING" ] && ok "PENDING" || bad "状态异常"
[ "$(SU "SELECT attempt_no FROM approvals WHERE ticket_id='$TKT';")" = "1" ] && ok "attempt_no=1" || bad "attempt 异常"
[ "$(SU "SELECT expires_at IS NULL FROM approvals WHERE ticket_id='$TKT';")" = "t" ] && ok "PENDING expires_at=NULL" || bad "PENDING expires_at 非 NULL"
[ "$(SU "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT';")" = "PENDING_DISPATCH" ] && ok "outbox 同事务 PENDING_DISPATCH" || bad "outbox 异常"

# 4c. pending_list 返回完整 payload(B4a.1 P1#3)
PL=$(APV "SELECT canonical_payload->>'merge_method' FROM l2_pending_list() WHERE ticket_id='$TKT';")
[ "$PL" = "squash" ] && ok "l2_pending_list 返回 canonical_payload(merge_method=squash 可见)" || bad "pending_list 缺 payload: '$PL'"
PL2=$(APV "SELECT expected_head_sha IS NOT NULL FROM l2_pending_list() WHERE ticket_id='$TKT';")
[ "$PL2" = "t" ] && ok "pending_list 返回 expected_head_sha + attempt" || bad "pending_list 缺字段"

# 4d. 审批
[ "$(APV "SELECT l2_approve('$TKT','tester@host');")" = "t" ] && ok "approve → APPROVED" || bad "approve 失败"
[ "$(SU "SELECT approved_by FROM approvals WHERE ticket_id='$TKT';")" = "tester@host" ] && ok "approved_by 记录" || bad "approved_by 缺"
[ "$(SU "SELECT expires_at > now() FROM approvals WHERE ticket_id='$TKT';")" = "t" ] && ok "expires_at 已写(+1h)" || bad "expires_at 未写"

# 4e. claim 错 hash → 0 行,保持 APPROVED
WRONG=$(python3 -c "print('a'*64)")
[ "$(L2 "SELECT count(*) FROM l2_claim_ticket('$TKT','merge','nghqqa/MergePilot',99999,'$WRONG');")" = "0" ] && ok "错 args_hash → 0 行" || bad "错 hash 不该消耗"
[ "$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';")" = "APPROVED" ] && ok "票据仍 APPROVED(CAS 未匹配不消耗)" || bad "票据被错误消耗"

# 4f. claim 正确 → EXECUTING + payload + execution_id
CL1=$(L2 "SELECT execution_id || '|' || canonical_payload::text FROM l2_claim_ticket('$TKT','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');")
EID="${CL1%%|*}"; PAY="${CL1#*|}"
echo "$EID" | grep -qE "^[0-9a-f-]{36}$" && ok "claim 返回 execution_id" || bad "claim 无 execution_id: $CL1"
echo "$PAY" | grep -qi "merge_method.*squash" && ok "claim 返回 canonical_payload(squash)" || bad "payload 异常"
[ "$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT';")" = "EXECUTING" ] && ok "EXECUTING" || bad "状态异常"

# 4g. complete → USED
[ "$(L2 "SELECT l2_complete_ticket('$TKT','$EID'::uuid,'mergesha123');")" = "t" ] && ok "complete → USED" || bad "complete 失败"
echo "$(SU "SELECT status||'|'||result_sha FROM approvals WHERE ticket_id='$TKT';")" | grep -q "USED|mergesha123" && ok "USED + result_sha" || bad "complete 状态异常"

# 4h. 第二票:fail 路径
TKT2=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);")
[ "$(SU "SELECT attempt_no FROM approvals WHERE ticket_id='$TKT2';")" = "2" ] && ok "第二票 attempt_no=2(advisory lock+MAX)" || bad "attempt_no 异常"
APV "SELECT l2_approve('$TKT2','tester@host');" >/dev/null
CL2=$(L2 "SELECT execution_id FROM l2_claim_ticket('$TKT2','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');")
[ "$(L2 "SELECT l2_fail_ticket('$TKT2','$CL2'::uuid,'github 409 conflict');")" = "t" ] && ok "fail_ticket → FAILED" || bad "fail 失败"
[ "$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT2';")" = "FAILED" ] && ok "FAILED" || bad "fail 状态异常"

# 4i. 第三票:mark_unknown + reconcile_unknown
TKT3=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);") ; APV "SELECT l2_approve('$TKT3','tester@host');" >/dev/null
CL3=$(L2 "SELECT execution_id FROM l2_claim_ticket('$TKT3','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');")
L2 "SELECT l2_mark_unknown('$TKT3','$CL3'::uuid,'network timeout');" >/dev/null
[ "$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT3';")" = "UNKNOWN" ] && ok "mark_unknown → UNKNOWN" || bad "mark_unknown 异常"
SU "SELECT l2_reconcile_unknown('$TKT3',true,'actualsha');" >/dev/null
[ "$(SU "SELECT status||'|'||result_sha FROM approvals WHERE ticket_id='$TKT3';")" = "USED|actualsha" ] && ok "reconcile_unknown(effect_applied=true)→ USED" || bad "reconcile_unknown 异常"

# 4j. 第四票:超时 EXECUTING reconcile + 120s 约束
TKT4=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);") ; APV "SELECT l2_approve('$TKT4','tester@host');" >/dev/null
CL4=$(L2 "SELECT execution_id FROM l2_claim_ticket('$TKT4','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');")
# 未过 120s → reconcile_executing 应 0 行(约束)
REARLY=$(SU "SELECT l2_reconcile_executing('$TKT4',false,'');")
[ "$REARLY" = "f" ] && ok "reconcile_executing 120s 内 → 拒(防提前对账)" || bad "reconcile_executing 未约束 120s"
# 手动改 executing_at 模拟超时
SU "UPDATE approvals SET executing_at = now() - interval '130 seconds' WHERE ticket_id='$TKT4';" >/dev/null
SU "SELECT l2_reconcile_executing('$TKT4',false,'');" >/dev/null
[ "$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT4';")" = "FAILED" ] && ok "reconcile_executing(超 120s,effect=false)→ FAILED" || bad "reconcile_executing 异常"

# 4k. expire_pending
TKT5=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);")
SU "UPDATE approvals SET approval_expires_at = now() - interval '1 minute' WHERE ticket_id='$TKT5';" >/dev/null
[ "$(SU "SELECT l2_expire_pending('$TKT5');")" = "t" ] && ok "expire_pending(超审批期)→ EXPIRED" || bad "expire 异常"
[ "$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT5';")" = "EXPIRED" ] && ok "EXPIRED" || bad "expire 状态异常"

# ─── 4z. B4a.2 加固:pgvector owner 恢复 + 精确 ACL + payload/hash/TTL 封闭 + 右方向 membership ───
log ""; log "=== 4z. B4a.2 加固 ==="
# pgvector owner 恢复(不应是 mergepilot_l2_owner)
PGV=$(SU "SELECT count(*) FROM pg_proc p JOIN pg_roles r ON p.proowner=r.oid JOIN pg_namespace n ON p.pronamespace=n.oid WHERE p.proname IN ('l2_distance','l2_norm','l2_normalize') AND n.nspname='public' AND r.rolname='mergepilot_l2_owner';")
[ "${PGV:-0}" = "0" ] && ok "pgvector 函数 owner 未被污染(allowlist 生效)" || bad "pgvector 仍被改成 l2_owner($PGV)"
PGV_OK=$(SU "SELECT count(*) FROM pg_proc p JOIN pg_roles r ON p.proowner=r.oid JOIN pg_namespace n ON p.pronamespace=n.oid WHERE p.proname IN ('l2_distance','l2_norm','l2_normalize') AND n.nspname='public' AND r.rolname='mergepilot';")
[ "${PGV_OK:-0}" != "0" ] && ok "pgvector 函数 owner = mergepilot(扩展 owner)" || bad "pgvector owner 未恢复到 mergepilot"

# 精确 ACL(不靠 superuser 旁路)
ACL_REC=$(SU "SELECT count(*) FROM information_schema.role_routine_grants WHERE routine_name='l2_reconcile_unknown' AND grantee='mergepilot';")
[ "${ACL_REC:-0}" = "1" ] && ok "l2_reconcile_unknown 显式 grant mergepilot EXECUTE(非 superuser 旁路)" || bad "reconcile 缺 mergepilot 显式 EXECUTE($ACL_REC)"
ACL_CRT=$(SU "SELECT count(*) FROM information_schema.role_routine_grants WHERE routine_name='l2_create_ticket' AND grantee='mergepilot';")
[ "${ACL_CRT:-0}" = "1" ] && ok "l2_create_ticket 显式 grant mergepilot" || bad "create 缺 mergepilot EXECUTE"
ACL_CLM=$(SU "SELECT count(*) FROM information_schema.role_routine_grants WHERE routine_name='l2_claim_ticket' AND grantee='policy_gateway_l2';")
[ "${ACL_CLM:-0}" = "1" ] && ok "l2_claim_ticket grantee=policy_gateway_l2" || bad "claim ACL 异常"
ACL_APV=$(SU "SELECT count(*) FROM information_schema.role_routine_grants WHERE routine_name='l2_approve' AND grantee='mergepilot_approver';")
[ "${ACL_APV:-0}" = "1" ] && ok "l2_approve grantee=mergepilot_approver" || bad "approve ACL 异常"

# payload/hash/TTL 封闭(负向)
chk_reject(){ local label="$1" sql="$2" pat="$3" res; res=$(SU "$sql" 2>&1); echo "$res" | grep -qi "$pat" && ok "$label" || bad "$label 未拒: $(echo "$res"|head -1)"; }
chk_reject "merge_method=octopus 拒" "SELECT l2_create_ticket('bnd-b4atest','merge','{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":99999,\"commit_title\":\"x\",\"merge_method\":\"octopus\"}'::jsonb,'$ARGS_HASH',24,1);" "merge_method"
chk_reject "args_hash 非 64hex 拒" "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'tooshort',24,1);" "64hex"
chk_reject "approval TTL 1000h 拒" "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',1000,1);" "approval TTL"
chk_reject "exec TTL 1000h 拒" "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1000);" "exec TTL"
chk_reject "merge payload 含 state 拒" "SELECT l2_create_ticket('bnd-b4atest','merge','{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":99999,\"commit_title\":\"x\",\"merge_method\":\"squash\",\"state\":\"closed\"}'::jsonb,'$ARGS_HASH',24,1);" "state"
chk_reject "merge payload 未知字段拒" "SELECT l2_create_ticket('bnd-b4atest','merge','{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":99999,\"commit_title\":\"x\",\"merge_method\":\"squash\",\"unexpected\":1}'::jsonb,'$ARGS_HASH',24,1);" "未知字段"
chk_reject "close 缺 state 拒" "SELECT l2_create_ticket('bnd-b4atest','close','{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":99999,\"title\":\"x\"}'::jsonb,'$ARGS_HASH',24,1);" "state"
chk_reject "action=revert 拒" "SELECT l2_create_ticket('bnd-b4atest','revert','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);" "merge/close"
# B4a.3 P1#B:JSON 类型校验(pullNumber 必须数字,commit_title 必须字符串)
chk_reject "pullNumber 字符串拒" "SELECT l2_create_ticket('bnd-b4atest','merge','{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":\"99999\",\"commit_title\":\"x\",\"merge_method\":\"squash\"}'::jsonb,'$ARGS_HASH',24,1);" "数字"
chk_reject "commit_title 对象拒" "SELECT l2_create_ticket('bnd-b4atest','merge','{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":99999,\"commit_title\":{\"x\":1},\"merge_method\":\"squash\"}'::jsonb,'$ARGS_HASH',24,1);" "字符串"

# 右方向 membership(三账号不被授予任何 role)+ owner 属性
MEM=$(SU "SELECT count(*) FROM pg_auth_members m WHERE m.member IN ('policy_gateway_l2'::regrole, 'mergepilot_approver'::regrole, 'mergepilot_l2_owner'::regrole);")
[ "${MEM:-0}" = "0" ] && ok "三账号无被授予的 membership(右方向,防 SET ROLE 越权)" || bad "有 membership($MEM)"
OWN_BAD=$(SU "SELECT count(*) FROM pg_roles WHERE rolname='mergepilot_l2_owner' AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication OR rolinherit OR rolcanlogin);")
[ "${OWN_BAD:-0}" = "0" ] && ok "mergepilot_l2_owner: NOLOGIN + NOINHERIT + 无高危属性" || bad "l2_owner 属性异常($OWN_BAD)"

# ─── 5. 真并发 claim(两个并行,只一个成功)───
log ""; log "=== 5. 真并发 claim(B4a.1 P2#9)==="
TKT6=$(SU "SELECT l2_create_ticket('bnd-b4atest','merge','$PAYLOAD'::jsonb,'$ARGS_HASH',24,1);") ; APV "SELECT l2_approve('$TKT6','tester@host');" >/dev/null
L2 "SELECT execution_id FROM l2_claim_ticket('$TKT6','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');" > /tmp/cc1.out 2>&1 &
L2 "SELECT execution_id FROM l2_claim_ticket('$TKT6','merge','nghqqa/MergePilot',99999,'$ARGS_HASH');" > /tmp/cc2.out 2>&1 &
wait
C1=$(grep -cE "^[0-9a-f-]{36}$" /tmp/cc1.out); C2=$(grep -cE "^[0-9a-f-]{36}$" /tmp/cc2.out)
TOT=$(( ${C1:-0} + ${C2:-0} ))
log "  并发 claim 成功数: $TOT(应 1)"
[ "$TOT" = "1" ] && ok "并发 claim 只一个成功(原子 CAS)" || bad "并发 claim 异常($TOT 成功)"

# ─── 6. 漂移收敛:误授函数后重跑脚本 → 自动撤销(B4a.1 P1#4)───
log ""; log "=== 6. 漂移收敛 ==="
SU "GRANT EXECUTE ON FUNCTION l2_approve(TEXT,TEXT) TO policy_gateway_l2;" >/dev/null  # 注入漂移:gateway 能 approve
DRIFT_BEFORE=$(SU "SELECT count(*) FROM information_schema.role_routine_grants WHERE grantee='policy_gateway_l2' AND routine_name='l2_approve';")
[ "$DRIFT_BEFORE" = "1" ] && ok "漂移注入(gateway 被误授 l2_approve)" || bad "漂移注入失败"
bash /mnt/d/goai/tools/m3b-b4-create-roles.sh >/dev/null 2>&1
DRIFT_AFTER=$(SU "SELECT count(*) FROM information_schema.role_routine_grants WHERE grantee='policy_gateway_l2' AND routine_name='l2_approve';")
[ "$DRIFT_AFTER" = "0" ] && ok "重跑角色脚本 → 漂移自动撤销" || bad "漂移未撤销($DRIFT_AFTER)"

# ─── 7. 账号隔离(再确认;用 capture-to-var 避免 pipefail 把 psql 失败码带进管道)───
log ""; log "=== 7. 账号隔离 ==="
T1=$(L2 "SELECT count(*) FROM approvals;"); echo "$T1" | grep -qi "permission denied" && ok "gateway_l2 不能 SELECT approvals" || bad "gateway SELECT 异常: $T1"
T2=$(APV "SELECT count(*) FROM policy_action_outbox;"); echo "$T2" | grep -qi "permission denied" && ok "approver 不能读 outbox" || bad "approver 读 outbox 异常: $T2"
T3=$(L2 "SELECT l2_approve('x','y');"); echo "$T3" | grep -qi "permission denied" && ok "gateway 越权 l2_approve 被拒" || bad "越权异常: $T3"

# 清理
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4atest-%'; DELETE FROM approvals WHERE run_id LIKE 'b4atest-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4atest-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4atest-%';" >/dev/null 2>&1

log ""
log "═══════════════════════════════════════════════"
log "  B4a+B4a.1 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ] || exit 1
