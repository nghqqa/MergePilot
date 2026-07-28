#!/bin/bash
# m3b-b4c5-e2e.sh — B4c-5 Controller 闭环验收(并发/崩溃/E2E + 收尾)。
# 覆盖:
#   1. 全链 E2E:verify PASS(APPROVAL_PENDING/l2_binding)→ initiate_l2_pending 发现+建票
#      → l2_approve → drain_l2_outbox → MERGED(真 merge;固化 result_sha + 恰好 1 次 L2_CLAIMED)。
#   2. DISPATCHED lease 后 Controller 崩溃 → 真容器 restart 恢复滞留 DISPATCHED → MERGED(attempts 1→2)。
#   3. 超时 EXECUTING 对账(l2_reconcile_executing 分支:B4c-4 只测 UNKNOWN):未合并→FAILED、已合并→USED。
#   4. UNKNOWN 对账 + **证明绝不重新 merge**:USED 收敛后再 drain/reconcile,mcp_calls L2_CLAIMED 计数仍 == 1。
#   5. 双 Controller 并发(两容器 run_forever):恰好 1 binding / 1 ticket / 1 outbox(attempts=1)/ 1 L2_CLAIMED。
#   6. Controller 级 DENY/异常:exec-TTL 过期→EXPIRED→HOLD(0 merge);Gateway CLAIM_MISMATCH→不 claim 不 merge。
# 证据先于清理(run_id/PR/SHA/DB 快照/日志/输出)。绝不重 merge 不变量以 mcp_calls 审计行计数为准。
set -uo pipefail
# ── Step 2 安全门 ──
# 本脚本在 B4c 闭合时固化于生产仓 nghqqa/MergePilot(frozen 证据脚本)。重跑会写生产仓
# → 默认拒。重跑需 export ALLOW_PRODUCTION_E2E=1(留痕);或迁 fixture(见 tools/e2e-lib.sh
# + evidence/m3b-b4c/step2-fixture/)。新 E2E(B4d+)默认走 fixture,不经此门。
source "$(dirname "$0")/e2e-lib.sh"
[ "${ALLOW_PRODUCTION_E2E:-0}" = "1" ] || { echo "REFUSED: $0 固化于生产仓 nghqqa/MergePilot;重跑需 ALLOW_PRODUCTION_E2E=1 或迁 fixture(见 e2e-lib.sh)" >&2; exit 2; }
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c/5-e2e
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out "$EV"/*.log 2>/dev/null || true
OUT="$EV/e2e-test.out"; : > "$OUT"
log(){ echo "$*" | tee -a "$OUT"; }
logf(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ log "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ log "  ❌ $1"; FAIL=$((FAIL+1)); }

DIR=/home/ngh/.config/mergepilot
CTRL="$DIR/controller.env"
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]'); PG_DB=${PG_DB:-mergepilot_audit}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
COORD=$(python3 -c "import json;print(json.load(open('$DIR/role-tokens.json')).get('coordinator',''))" 2>/dev/null || echo "")
source "$DIR/audit-db.env" 2>/dev/null
AUDIT_DSN_VAL="postgresql://${PGW_AUDIT_USER}:${PGW_AUDIT_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
source "$DIR/b4-roles.env" 2>/dev/null
L2_DSN_VAL="postgresql://${POLICY_GATEWAY_L2_USER}:${POLICY_GATEWAY_L2_PASS}@audit-pg:5432/${PGW_AUDIT_DB}"
ROLE_TOKENS_VAL=$(cat "$DIR/role-tokens.json")
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
GW(){ docker exec policy-gw python3 /tmp/probe-tools.py "${@}" 2>&1; }
IMG=mergepilot-controller:latest
NAME=mergepilot-controller
NAME2=mergepilot-controller-b
TS=$$
ENVF="$DIR/controller.env"
NET=--network=hiclab-net
ENV_ARGS=(--env-file "$ENVF" -e PG_HOST=audit-pg -e PG_PORT=5432 -e PG_DATABASE=mergepilot_audit -e PG_USER=mergepilot \
  -e MATRIX_HS=http://hiclaw-controller:6167 -e GATEWAY_URL=http://policy-gw:8083 -e COORDINATOR_TOKEN="$COORD" -e L2_MERGE_ENABLED=0)

cleanup_runs(){ PSQL "DELETE FROM mcp_calls WHERE run_id LIKE 'b4c5-%'; DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4c5-%'; DELETE FROM approvals WHERE run_id LIKE 'b4c5-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4c5-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4c5-%';" >/dev/null 2>&1 || true; }
# 注:mcp_calls DELETE 被 mcp_calls_immutable() 触发器拦截(B3 INSERT-only 审计,superuser 亦不可删);
#     上行 mcp_calls 清理为"尽力而为"——审计跨测试运行持久累积,per-ticket L2_CLAIMED 计数以测试内联断言为准。
restore(){ docker rm -f "$NAME2" policy-gw-fault-unk 2>/dev/null; docker start "$NAME" >/dev/null 2>&1 || true; cleanup_runs; }
trap restore EXIT

log "═══════════════════════════════════════════════"
log "  B4c-5 Controller 闭环验收(并发/崩溃/E2E)"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker cp /mnt/d/goai/mergepilot-os/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py >/dev/null 2>&1
docker build -t "$IMG" /mnt/d/goai/mergepilot-os/tools/workflow-controller/ >>"$OUT" 2>&1 || { bad "镜像 build 失败"; exit 1; }
for f in controller.py gateway_client.py; do
  ch=$(docker run --rm "$IMG" python3 -c "import hashlib;print(hashlib.sha256(open('/app/$f','rb').read()).hexdigest()[:16])" 2>/dev/null)
  rh=$(sha256sum "/mnt/d/goai/mergepilot-os/tools/workflow-controller/$f" | cut -c1-16)
  [ "$ch" = "$rh" ] && ok "$f 容器内==仓库" || bad "$f 漂移"
done
# (B4c-5 异常路径用真实"超时其实已合并"构造,不经 fault gateway;故不创建 policy-gw-fault-unk)

docker stop "$NAME" >/dev/null 2>&1 || true   # 一次性测试阶段:停主控制器,防干扰
cleanup_runs

# 一次性容器调 controller: $1=python_expr, $2=GATEWAY_URL(默认真实)
# 原始输出(含 [ctrl] 日志/traceback)追加到 run-py-raw.log 供排障;只回显 STATUS=/INFO=
run_py(){ local GWU="${2:-http://policy-gw:8083}"
  docker run --rm $NET "${ENV_ARGS[@]}" -e GATEWAY_URL="$GWU" -e L2_GW_TIMEOUT=15 \
    "$IMG" python3 -c "$1" >> "$EV/run-py-raw.log" 2>&1
  grep -E "^STATUS=|^INFO=" "$EV/run-py-raw.log" | tail -2 || true; }
# initiate_l2_pending 单次只跑 discover 或 ticket(候选集在 tick 起点快照);
# 调多次模拟主循环多 tick,使 l2_binding→l2_awaiting_ticket→l2_awaiting_approval 一气走完
init_ticks(){ run_py "import controller
for _ in range(5): controller.initiate_l2_pending()"; }
create_fix_pr(){ local BR="$1" P="$2" L="$3" R
  GW fixer --call create_branch owner=nghqqa repo=MergePilot branch="$BR" from_branch=main 2>&1 | grep -qi ref && logf "  分支 $BR 建好"
  GW fixer --call create_or_update_file owner=nghqqa repo=MergePilot path="$P" branch="$BR" content="b4c5-$L-$TS" message="b4c5 $L" 2>&1 | grep -qi "commit\|sha" && logf "  commit 加好"
  R=$(GW fixer --call create_pull_request owner=nghqqa repo=MergePilot head="$BR" base=main title="B4c-5 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
mkrun(){ PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$1','APPROVAL_PENDING','nghqqa/MergePilot',0,'l2_binding',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_binding',approval_required=TRUE;" >/dev/null; }
# 恰好一次 L2 claim(= 一次真 merge)的权威计数
CLAIM_CNT(){ PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='$1' AND reason_code='L2_CLAIMED';"; }
DENY_CNT(){ PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='$1' AND reason_code='CLAIM_MISMATCH';"; }

# ════════════ 1. 全链 E2E(经 initiate_l2_pending 主循环入口)════════════
log ""; log "=== 1. 全链 E2E: l2_binding → 发现+建票 → approve → drain → MERGED ==="
RUN1=b4c5-e2e-$TS
mkrun "$RUN1"; PR1=$(create_fix_pr "fix/$RUN1-x" "e2e-$TS.md" "e2e")
if [ -z "$PR1" ]; then bad "E2E: PR 创建失败"; else
  logf "  run=$RUN1 pr=#$PR1"
  # 主循环入口:discover+ticket(advisory lock 路径),不直调 discover/create_ticket
  init_ticks >/dev/null
  ST1A=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN1';")
  BID1=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN1';")
  TKT1=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID1';")
  HEAD1=$(PSQL "SELECT head_sha FROM run_pr_bindings WHERE run_id='$RUN1';")
  [ "$ST1A" = "l2_awaiting_approval" ] && ok "initiate_l2_pending: l2_binding → l2_awaiting_approval(发现+建票)" || bad "阶段异常: $ST1A"
  [ -n "$BID1" ] && ok "binding 写入(head_sha=${HEAD1:0:12})" || bad "无 binding"
  [ -n "$TKT1" ] && ok "ticket 建票(单张)" || bad "无 ticket"
  # approve(模拟审批;B4d 才有 CLI)
  PSQL "SELECT l2_approve('$TKT1','b4c5-e2e@host');" >/dev/null
  AST1a=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  [ "$AST1a" = "APPROVED" ] && ok "l2_approve: PENDING → APPROVED" || bad "approve 异常: $AST1a"
  # drain → 真 merge
  run_py "import controller; controller.drain_l2_outbox()" >/dev/null
  AST1=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  OST1=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT1';")
  TST1=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN1';")
  CST1=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN1';")
  SHA1=$(PSQL "SELECT result_sha FROM approvals WHERE ticket_id='$TKT1';")
  CC1=$(CLAIM_CNT "$TKT1")
  logf "  approval=$AST1 outbox=$OST1 task=$TST1 stage=$CST1 sha=${SHA1:0:12} claims=$CC1"
  [ "$AST1" = "USED" ] && ok "approval → USED" || bad "approval 应 USED: $AST1"
  [ "$OST1" = "SUCCEEDED" ] && ok "outbox → SUCCEEDED" || bad "outbox 应 SUCCEEDED: $OST1"
  [ "$TST1" = "MERGED" ] && ok "task → MERGED" || bad "task 应 MERGED: $TST1"
  [ "$CST1" = "l2_done" ] && ok "current_stage → l2_done" || bad "stage 异常: $CST1"
  [ -n "$SHA1" ] && ok "result_sha 固化(merge commit ${SHA1:0:12})" || bad "result_sha 空"
  [ "$CC1" = "1" ] && ok "恰好 1 次 L2_CLAIMED(审计可追溯)" || bad "L2_CLAIMED 计数异常: $CC1(应 1)"
fi

# ════════════ 3. 超时 EXECUTING 对账(l2_reconcile_executing 分支)════════════
log ""; log "=== 2. 超时 EXECUTING 对账(reconcile_executing;未合并→FAILED / 已合并→USED) ==="
setup_executing(){ local RUN="$1" BR="$2" P="$3" L="$4" PR TKT BID
  mkrun "$RUN"; PR=$(create_fix_pr "$BR" "$P" "$L"); [ -z "$PR" ] && { echo ""; return; }
  init_ticks >/dev/null 2>&1
  BID=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN';")
  TKT=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID';")
  PSQL "UPDATE approvals SET status='EXECUTING', execution_id=gen_random_uuid(), executing_at=now()-interval '200 seconds' WHERE ticket_id='$TKT';" >/dev/null
  echo "$TKT"; }
# 3a. EXECUTING + 未合并 → reconcile → FAILED
RUN3a=b4c5-execfail-$TS
TKT3a=$(setup_executing "$RUN3a" "fix/$RUN3a-x" "execfail-$TS.md" "execfail")
if [ -z "$TKT3a" ]; then bad "EXEC-FAIL: setup 失败"; else
  run_py "import controller; controller.reconcile_l2()" >/dev/null
  AST3a=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT3a';")
  OST3a=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT3a';")
  TST3a=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN3a';")
  logf "  approval=$AST3a outbox=$OST3a task=$TST3a"
  [ "$AST3a" = "FAILED" ] && ok "EXECUTING 超时(未合并)→ FAILED" || bad "应 FAILED: $AST3a"
  [ "$OST3a" = "FAILED" ] && ok "outbox → FAILED(收敛)" || bad "outbox 应 FAILED: $OST3a"
  [ "$TST3a" = "HOLD" ] && ok "task → HOLD" || bad "task 应 HOLD: $TST3a"
fi
# 3b. EXECUTING + 已合并 → reconcile → USED → MERGED
RUN3b=b4c5-execused-$TS
TKT3b=$(setup_executing "$RUN3b" "fix/$RUN3b-x" "execused-$TS.md" "execused")
if [ -z "$TKT3b" ]; then bad "EXEC-USED: setup 失败"; else
  BID3b=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN3b';")
  PR3b=$(PSQL "SELECT pr_number FROM run_pr_bindings WHERE run_id='$RUN3b';")
  PAY3b='{"owner":"nghqqa","repo":"MergePilot","pullNumber":'$PR3b',"commit_title":"execused","merge_method":"squash"}'
  AH3b=$(python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$PAY3b")
  PSQL "UPDATE approvals SET canonical_payload='$PAY3b'::jsonb, args_hash='$AH3b', status='APPROVED', expires_at=now()+interval '1 hour' WHERE ticket_id='$TKT3b';" >/dev/null
  GW coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$PR3b commit_title="execused" merge_method=squash approval_ticket=$TKT3b >/dev/null 2>&1
  # 改回 EXECUTING 超时(Gateway 已 USED;模拟"超时但实际已成功")
  PSQL "UPDATE approvals SET status='EXECUTING', executing_at=now()-interval '200 seconds' WHERE ticket_id='$TKT3b';" >/dev/null
  PSQL "UPDATE policy_action_outbox SET status='DISPATCHED' WHERE ticket_id='$TKT3b';" >/dev/null
  run_py "import controller; controller.reconcile_l2()" >/dev/null
  AST3b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT3b';")
  OST3b=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT3b';")
  TST3b=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN3b';")
  logf "  approval=$AST3b outbox=$OST3b task=$TST3b"
  [ "$AST3b" = "USED" ] && ok "EXECUTING 超时(已合并)→ USED" || bad "应 USED: $AST3b"
  [ "$OST3b" = "SUCCEEDED" ] && ok "outbox → SUCCEEDED(收敛)" || bad "outbox 应 SUCCEEDED: $OST3b"
  [ "$TST3b" = "MERGED" ] && ok "task → MERGED(收敛)" || bad "task 应 MERGED: $TST3b"
fi

# ════════════ 4. UNKNOWN 对账 + 证明绝不重新 merge ════════════
log ""; log "=== 3. UNKNOWN 对账 + 绝不重新 merge(L2_CLAIMED 计数不变) ==="
RUN4=b4c5-noremerge-$TS
TKT4=$(setup_executing "$RUN4" "fix/$RUN4-x" "noremerge-$TS.md" "noremerge")
if [ -z "$TKT4" ]; then bad "NO-REMERGE: setup 失败"; else
  BID4=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN4';")
  PR4=$(PSQL "SELECT pr_number FROM run_pr_bindings WHERE run_id='$RUN4';")
  PAY4='{"owner":"nghqqa","repo":"MergePilot","pullNumber":'$PR4',"commit_title":"noremerge","merge_method":"squash"}'
  AH4=$(python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$PAY4")
  PSQL "UPDATE approvals SET canonical_payload='$PAY4'::jsonb, args_hash='$AH4', status='APPROVED', expires_at=now()+interval '1 hour' WHERE ticket_id='$TKT4';" >/dev/null
  # 真合并一次(write_timeout 当时其实已成功 → UNKNOWN)
  GW coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$PR4 commit_title="noremerge" merge_method=squash approval_ticket=$TKT4 >/dev/null 2>&1
  PSQL "UPDATE approvals SET status='UNKNOWN', executing_at=now()-interval '200 seconds' WHERE ticket_id='$TKT4';" >/dev/null
  PSQL "UPDATE policy_action_outbox SET status='UNKNOWN' WHERE ticket_id='$TKT4';" >/dev/null
  CC4_BEFORE=$(CLAIM_CNT "$TKT4")
  run_py "import controller; controller.reconcile_l2()" >/dev/null   # UNKNOWN → USED(已合并)
  AST4=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT4';")
  TST4=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN4';")
  [ "$AST4" = "USED" ] && ok "UNKNOWN(已合并)→ USED" || bad "应 USED: $AST4"
  [ "$TST4" = "MERGED" ] && ok "task → MERGED" || bad "task 应 MERGED: $TST4"
  # 再 drain + 再 reconcile:不应触发第二次 merge(USED 不在 drain 候选;unique index 拒新建票)
  run_py "import controller; controller.drain_l2_outbox(); controller.reconcile_l2()" >/dev/null
  CC4_AFTER=$(CLAIM_CNT "$TKT4")
  logf "  L2_CLAIMED 计数: 收敛前=$CC4_BEFORE  收敛后再 drain/reconcile 后=$CC4_AFTER"
  [ "$CC4_AFTER" = "$CC4_BEFORE" ] && ok "绝不重新 merge(L2_CLAIMED 计数不变=$CC4_AFTER)" || bad "重 merge 了: $CC4_BEFORE→$CC4_AFTER"
  # 第二张票被 unique index 拒(USED 在阻塞集)——裸调 l2_create_ticket;PSQL helper 吞 stderr,
  # 故按"总数是否不变"判定(被拒则事务回滚,approvals 行数不变)
  NT4_BEFORE=$(PSQL "SELECT count(*) FROM approvals WHERE binding_id='$BID4' AND action='merge';")
  PSQL "SELECT l2_create_ticket('$BID4','merge','$PAY4'::jsonb,'$AH4',24,1);" >/dev/null 2>&1
  NT4_AFTER=$(PSQL "SELECT count(*) FROM approvals WHERE binding_id='$BID4' AND action='merge';")
  [ "$NT4_AFTER" = "$NT4_BEFORE" ] && ok "USED 后建第二张票被拒(uq_active_ticket_per_binding_action,总数不变=$NT4_AFTER)" || bad "第二张票未被拒: $NT4_BEFORE→$NT4_AFTER"
fi

# ════════════ 6. Controller 级 DENY / 异常路径 ════════════
log ""; log "=== 4. Controller 级 DENY:exec-TTL 过期 → EXPIRED → HOLD(0 merge) ==="
RUN6a=b4c5-denyexpire-$TS
TKT6a=$(setup_executing "$RUN6a" "fix/$RUN6a-x" "denyexp-$TS.md" "denyexpire")
if [ -z "$TKT6a" ]; then bad "DENY-EXPIRE: setup 失败"; else
  # 改成 APPROVED 但执行期 expires_at 已过 → Gateway claim 拒(expires_at<now)→ reconcile l2_expire_approved → EXPIRED
  PSQL "UPDATE approvals SET status='APPROVED', approved_at=now()-interval '3 hours', expires_at=now()-interval '2 hours' WHERE ticket_id='$TKT6a';" >/dev/null
  run_py "import controller; controller.drain_l2_outbox(); controller.reconcile_l2()" >/dev/null
  AST6a=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT6a';")
  OST6a=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT6a';")
  TST6a=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN6a';")
  CC6a=$(CLAIM_CNT "$TKT6a")
  logf "  approval=$AST6a outbox=$OST6a task=$TST6a claims=$CC6a"
  [ "$AST6a" = "EXPIRED" ] && ok "exec-TTL 过期 → EXPIRED(Controller 拒执行)" || bad "应 EXPIRED: $AST6a"
  [ "$OST6a" = "FAILED" ] && ok "outbox → FAILED" || bad "outbox 应 FAILED: $OST6a"
  [ "$TST6a" = "HOLD" ] && ok "task → HOLD(未 MERGED)" || bad "task 应 HOLD: $TST6a"
  [ "$CC6a" = "0" ] && ok "0 次 L2_CLAIMED(从未 merge)" || bad "不应有 claim: $CC6a"
fi
log ""; log "=== 5. Gateway CLAIM_MISMATCH 异常 → 不 claim / 不 merge ==="
RUN6b=b4c5-mismatch-$TS
TKT6b=$(setup_executing "$RUN6b" "fix/$RUN6b-x" "mismatch-$TS.md" "mismatch")
if [ -z "$TKT6b" ]; then bad "MISMATCH: setup 失败"; else
  PR6b=$(PSQL "SELECT pr_number FROM run_pr_bindings WHERE run_id='$RUN6b';")
  PSQL "UPDATE approvals SET status='APPROVED', expires_at=now()+interval '1 hour' WHERE ticket_id='$TKT6b';" >/dev/null
  # 故意用与票(存 squash)不同的 merge_method 调 merge → args_hash 不匹配 → CLAIM_MISMATCH,票留 APPROVED
  GW coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$PR6b commit_title="mismatch" merge_method=merge approval_ticket=$TKT6b >/dev/null 2>&1
  AST6b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT6b';")
  TST6b=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN6b';")
  DC6b=$(DENY_CNT "$TKT6b"); CC6b=$(CLAIM_CNT "$TKT6b")
  logf "  approval=$AST6b task=$TST6b deny=$DC6b claims=$CC6b"
  [ "$AST6b" = "APPROVED" ] && ok "CLAIM_MISMATCH: 票仍 APPROVED(未 USED)" || bad "票异常: $AST6b"
  [ "$TST6b" = "APPROVAL_PENDING" ] && ok "task 未推进(不 MERGED)" || bad "task 异常: $TST6b"
  { [ "$DC6b" -ge "1" ] || [ "$CC6b" = "0" ]; } && ok "Gateway 拒 claim(CLAIM_MISMATCH 或 0 claim)" || bad "异常:deny=$DC6b claims=$CC6b"
  # 中和 RUN6b 的 outbox(置 FAILED),防后续"运行中控制器"测试把它当 drain 候选误合并
  PSQL "UPDATE policy_action_outbox SET status='FAILED', error='test-neutralized after CLAIM_MISMATCH assert' WHERE ticket_id='$TKT6b';" >/dev/null
fi

# ════════════ 2. DISPATCHED lease 后崩溃 → 真容器 restart 恢复 ════════════
log ""; log "=== 6. DISPATCHED lease 后 Controller 崩溃 → 真容器 restart 恢复 ==="
RUN2=b4c5-crash-$TS
TKT2=$(setup_executing "$RUN2" "fix/$RUN2-x" "crash-$TS.md" "crash")
if [ -z "$TKT2" ]; then bad "CRASH: setup 失败"; else
  PR2=$(PSQL "SELECT pr_number FROM run_pr_bindings WHERE run_id='$RUN2';")
  PAY2='{"owner":"nghqqa","repo":"MergePilot","pullNumber":'$PR2',"commit_title":"crash","merge_method":"squash"}'
  AH2=$(python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$PAY2")
  PSQL "UPDATE approvals SET canonical_payload='$PAY2'::jsonb, args_hash='$AH2', status='APPROVED', expires_at=now()+interval '2 hours' WHERE ticket_id='$TKT2';" >/dev/null
  # 领取(DISPATCHED+lease,提交)→ bad gateway(模拟"dispatch 后崩溃,Gateway 未应答")
  run_py "import controller; controller.drain_l2_outbox()" "http://policy-gw-unreachable:9999" >/dev/null
  OST2=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  LEASE2=$(PSQL "SELECT lease_expires_at IS NOT NULL FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  ATT2=$(PSQL "SELECT attempts FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  logf "  崩溃点: outbox=$OST2 lease_set=$LEASE2 attempts=$ATT2(应 DISPATCHED/t/1)"
  [ "$OST2" = "DISPATCHED" ] && ok "crash 后 outbox 滞留 DISPATCHED" || bad "outbox 异常: $OST2"
  [ "$LEASE2" = "t" ] && ok "lease 已写入(领取时)" || bad "lease 未写"
  [ "$ATT2" = "1" ] && ok "attempts=1(首次领取)" || bad "attempts 异常: $ATT2"
  # 模拟 lease 已过(时间流逝);restart 真容器(run_forever)恢复
  PSQL "UPDATE policy_action_outbox SET lease_expires_at=now()-interval '1 minute' WHERE ticket_id='$TKT2';" >/dev/null
  docker start "$NAME" >/dev/null 2>&1
  recovered=0
  for i in $(seq 1 12); do   # 等真容器 drain 恢复(~POLL_INTERVAL 8s)
    TST2x=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN2';")
    [ "$TST2x" = "MERGED" ] && { recovered=1; break; }
    sleep 5
  done
  ATT2b=$(PSQL "SELECT attempts FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  AST2b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT2';")
  OST2b=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  SHA2=$(PSQL "SELECT result_sha FROM approvals WHERE ticket_id='$TKT2';")
  CC2=$(CLAIM_CNT "$TKT2")
  logf "  恢复后: task=$TST2x approval=$AST2b outbox=$OST2b attempts=$ATT2b sha=${SHA2:0:12} claims=$CC2"
  [ "$recovered" = "1" ] && ok "真容器 restart 恢复滞留 DISPATCHED → MERGED" || bad "未恢复(task=$TST2x)"
  [ "$ATT2b" = "2" ] && ok "attempts 1→2(lease 重派计数)" || bad "attempts 异常: $ATT2b(应 2)"
  [ "$AST2b" = "USED" ] && ok "approval → USED" || bad "approval 应 USED: $AST2b"
  [ "$CC2" = "1" ] && ok "恰好 1 次 L2_CLAIMED(恢复未重 merge)" || bad "L2_CLAIMED 异常: $CC2(应 1)"
  docker stop "$NAME" >/dev/null 2>&1 || true   # 停回,供后续并发测试
fi

# ════════════ 5. 双 Controller 并发(两容器 run_forever)════════════
log ""; log "=== 7. 双 Controller 并发:1 binding / 1 ticket / 1 merge ==="
RUN5=b4c5-conc-$TS
mkrun "$RUN5"; PR5=$(create_fix_pr "fix/$RUN5-x" "conc-$TS.md" "conc")
if [ -z "$PR5" ]; then bad "CONCURRENT: PR 创建失败"; else
  docker rm -f "$NAME2" >/dev/null 2>&1
  docker run -d --name "$NAME2" $NET --restart no "${ENV_ARGS[@]}" -e POLL_INTERVAL=3 "$IMG" >/dev/null 2>&1
  docker start "$NAME" >/dev/null 2>&1   # 主控制器也起(两实例并发)
  # 等两容器都过 startup_assert_l2 并进入主循环(首个 tick 前)
  for i in $(seq 1 10); do H1=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || echo na); H2=$(docker inspect -f '{{.State.Status}}' "$NAME2" 2>/dev/null || echo na); { [ "$H1" = "running" ] && [ "$H2" = "running" ]; } && break; sleep 2; done
  sleep 6   # 让 startup_assert_l2 + 首个 L2 tick 跑起来
  # 两控制器并发跑 discover+ticket(都从 l2_binding 起);轮询直到建票完成
  ticketed=0
  for i in $(seq 1 12); do
    CST5=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN5';")
    [ "$CST5" = "l2_awaiting_approval" ] && { ticketed=1; break; }
    sleep 3
  done
  NB5=$(PSQL "SELECT count(*) FROM run_pr_bindings WHERE run_id='$RUN5';")
  NT5=$(PSQL "SELECT count(*) FROM approvals WHERE run_id='$RUN5' AND status IN ('PENDING','APPROVED','EXECUTING','UNKNOWN','USED');")
  logf "  并发发现+建票后: bindings=$NB5 tickets=$NT5 stage=$CST5"
  [ "$NB5" = "1" ] && ok "并发下恰好 1 binding(uq_run_pr_bindings_run)" || bad "binding 数异常: $NB5"
  [ "$NT5" = "1" ] && ok "并发下恰好 1 ticket(uq_active_ticket_per_binding_action)" || bad "ticket 数异常: $NT5"
  TKT5=$(PSQL "SELECT ticket_id FROM approvals WHERE run_id='$RUN5';")
  PSQL "SELECT l2_approve('$TKT5','b4c5-conc@host');" >/dev/null
  # 两控制器并发 drain;轮询直到 MERGED
  merged=0
  for i in $(seq 1 12); do
    TST5=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN5';")
    [ "$TST5" = "MERGED" ] && { merged=1; break; }
    sleep 3
  done
  ATT5=$(PSQL "SELECT attempts FROM policy_action_outbox WHERE ticket_id='$TKT5';")
  CC5=$(CLAIM_CNT "$TKT5")
  logf "  并发 drain 后: task=$TST5 attempts=$ATT5 claims=$CC5"
  [ "$merged" = "1" ] && ok "并发 drain → MERGED" || bad "未 MERGED: $TST5"
  [ "$ATT5" = "1" ] && ok "并发领取只 +1(FOR UPDATE SKIP LOCKED 互斥)" || bad "attempts 异常: $ATT5(应 1)"
  [ "$CC5" = "1" ] && ok "并发下恰好 1 次 L2_CLAIMED(无重复 merge)" || bad "L2_CLAIMED 异常: $CC5(应 1)"
  docker stop "$NAME" "$NAME2" >/dev/null 2>&1 || true
fi

# ════════════ 证据固化 ════════════
log ""; log "=== 证据固化 ==="
PSQL "SELECT t.run_id, t.status AS task, t.current_stage, a.status AS appr, o.status AS outbox, o.attempts, a.result_sha
       FROM task_runs t LEFT JOIN approvals a ON a.run_id=t.run_id
       LEFT JOIN policy_action_outbox o ON o.run_id=t.run_id
       WHERE t.run_id LIKE 'b4c5-%' ORDER BY t.run_id;" > "$EV/db-snapshot.txt" 2>/dev/null
PSQL "SELECT ticket_id, tool, decision, reason_code, substr(error,1,40) FROM mcp_calls
       WHERE ticket_id IN (SELECT ticket_id FROM approvals WHERE run_id LIKE 'b4c5-%') ORDER BY ts;" > "$EV/mcp-calls.txt" 2>/dev/null
docker logs "$NAME" 2>&1 | tail -80 > "$EV/controller-A-logs.txt" 2>/dev/null || true
docker logs policy-gw 2>&1 | tail -60 > "$EV/gateway-logs.txt" 2>/dev/null || true
{ echo "b4c5 E2E residue branches (real GitHub PRs created/merged on nghqqa/MergePilot):"; \
  PSQL "SELECT run_id, pr_number, status FROM task_runs WHERE run_id LIKE 'b4c5-%' ORDER BY run_id;"; } > "$EV/github-residue.txt" 2>/dev/null
set +e; grep -rniE "token=[A-Za-z0-9]{8}|Bearer [A-Za-z0-9]{8}|sk-live|access_token" "$EV" > "$EV/credential-scan.txt" 2>/dev/null; GR=$?
[ "$GR" -ne 0 ] && { : > "$EV/credential-scan.txt"; ok "无凭证泄漏"; } || bad "凭证泄漏"

cleanup_runs
trap - EXIT
log ""
log "═══════════════════════════════════════════════"
log "  B4c-5 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
docker start "$NAME" >/dev/null 2>&1 || true
[ "$FAIL" -eq 0 ] || exit 1
