#!/bin/bash
# m3b-b4c4-reconcile.sh — B4c-4 延迟对账验收(确定性 fixture,不靠 timeout 真合并)。
# 覆盖:UNKNOWN + GitHub 未合并→FAILED / UNKNOWN + GitHub 已合并→USED / PENDING 过期→EXPIRED+HOLD /
#   滞留 DISPATCHED(approval=USED)→ outbox SUCCEEDED / close 收紧(merged=true vs state=closed AND merged=false)。
set -uo pipefail
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c/4-reconcile
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out 2>/dev/null || true
OUT="$EV/reconcile-test.out"; : > "$OUT"
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
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
GW(){ docker exec policy-gw python3 /tmp/probe-tools.py "${@}" 2>&1; }
IMG=mergepilot-controller:latest
NAME=mergepilot-controller
TS=$$
ENVF="$DIR/controller.env"
cleanup_runs(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4c4-%'; DELETE FROM approvals WHERE run_id LIKE 'b4c4-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4c4-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4c4-%';" >/dev/null 2>&1 || true; }
restore(){ docker start "$NAME" >/dev/null 2>&1 || true; cleanup_runs; }
trap restore EXIT

log "═══════════════════════════════════════════════"
log "  B4c-4 延迟对账验收"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker cp /mnt/d/goai/mergepilot-os/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py >/dev/null 2>&1
docker build -t "$IMG" /mnt/d/goai/mergepilot-os/tools/workflow-controller/ >>"$OUT" 2>&1 || { bad "镜像 build 失败"; exit 1; }
for f in controller.py; do
  ch=$(docker run --rm "$IMG" python3 -c "import hashlib;print(hashlib.sha256(open('/app/$f','rb').read()).hexdigest()[:16])" 2>/dev/null)
  rh=$(sha256sum "/mnt/d/goai/mergepilot-os/tools/workflow-controller/$f" | cut -c1-16)
  [ "$ch" = "$rh" ] && ok "$f 容器内==仓库" || bad "$f 漂移"
done
docker stop "$NAME" >/dev/null 2>&1 || true
cleanup_runs

run_py(){ docker run --rm --network hiclab-net --env-file "$ENVF" -e PG_HOST=audit-pg -e PG_DATABASE=mergepilot_audit \
  -e PG_USER=mergepilot -e GATEWAY_URL=http://policy-gw:8083 -e COORDINATOR_TOKEN="$COORD" -e L2_MERGE_ENABLED=0 -e L2_GW_TIMEOUT=30 \
  "$IMG" python3 -c "$1" 2>&1 | grep -vE "^\[ctrl\]"; }
reconcile(){ run_py "import controller; controller.reconcile_l2()"; }
create_fix_pr(){ local BR="$1" P="$2" L="$3" R
  GW fixer --call create_branch owner=nghqqa repo=MergePilot branch="$BR" from_branch=main 2>&1 | grep -qi ref && logf "  分支 $BR 建好"
  GW fixer --call create_or_update_file owner=nghqqa repo=MergePilot path="$P" branch="$BR" content="b4c4-$L-$TS" message="b4c4 $L" 2>&1 | grep -qi "commit\|sha" && logf "  commit 加好"
  R=$(GW fixer --call create_pull_request owner=nghqqa repo=MergePilot head="$BR" base=main title="B4c-4 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
mkrun(){ PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$1','APPROVAL_PENDING','nghqqa/MergePilot',0,'l2_binding',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_binding',approval_required=TRUE;" >/dev/null; }

# helper:构 UNKNOWN 票(executing_at >120s 前)+ run 在 APPROVAL_PENDING
setup_unknown_ticket(){ local RUN="$1" BR="$2" P="$3" L="$4"
  mkrun "$RUN"; local PR=$(create_fix_pr "$BR" "$P" "$L")
  [ -z "$PR" ] && { echo ""; return; }
  run_py "import controller; controller.discover_binding_for_run('$RUN'); controller.create_ticket_for_run('$RUN')" >/dev/null
  local BID=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN';")
  local TKT=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID';")
  # 模拟 write_timeout → UNKNOWN(executing_at>120s 前,满足 reconcile 延迟条件)
  PSQL "UPDATE approvals SET status='UNKNOWN', execution_id=gen_random_uuid(), executing_at=now()-interval '200 seconds' WHERE ticket_id='$TKT';" >/dev/null
  echo "$TKT"; }

# ─── 1. UNKNOWN + GitHub 未合并 → reconcile → FAILED ───
log ""; log "=== 1. UNKNOWN + 未合并 → FAILED ==="
RUN1=b4c4-unkfail-$TS
TKT1=$(setup_unknown_ticket "$RUN1" "fix/$RUN1-x" "rec-unkf-$TS.md" "unkfail")
if [ -z "$TKT1" ]; then bad "UNKNOWN-FAIL: setup 失败"; else
  reconcile >/dev/null
  AST1=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  OST1=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT1';")
  TST1=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN1';")
  logf "  approval=$AST1 outbox=$OST1 task=$TST1(PR 未 merged → FAILED)"
  [ "$AST1" = "FAILED" ] && ok "UNKNOWN + 未合并 → FAILED" || bad "应 FAILED: $AST1"
  [ "$OST1" = "FAILED" ] && ok "outbox → FAILED(收敛)" || bad "outbox 应 FAILED: $OST1"
  [ "$TST1" = "HOLD" ] && ok "task → HOLD(收敛)" || bad "task 应 HOLD: $TST1"
fi

# ─── 2. UNKNOWN + GitHub 已合并 → reconcile → USED ───
log ""; log "=== 2. UNKNOWN + 已合并 → USED ==="
RUN2=b4c4-unkused-$TS
TKT2=$(setup_unknown_ticket "$RUN2" "fix/$RUN2-x" "rec-unku-$TS.md" "unkused")
if [ -z "$TKT2" ]; then bad "UNKNOWN-USED: setup 失败"; else
  # 真合并 PR(经 gateway coordinator),模拟"write_timeout 时 merge 其实已成功"
  BID2=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN2';")
  PR2=$(PSQL "SELECT pr_number FROM run_pr_bindings WHERE run_id='$RUN2';")
  PAYLOAD2='{"owner":"nghqqa","repo":"MergePilot","pullNumber":'$PR2',"commit_title":"rec unkused","merge_method":"squash"}'
  AH2=$(python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$PAYLOAD2")
  # update ticket payload to match the merge args (票的 canonical_payload 必须与调用一致)
  PSQL "UPDATE approvals SET canonical_payload='$PAYLOAD2'::jsonb, args_hash='$AH2', status='APPROVED', expires_at=now()+interval '1 hour' WHERE ticket_id='$TKT2';" >/dev/null
  # 真 merge via gateway(让 PR merged=true)
  GW coordinator --call merge_pull_request owner=nghqqa repo=MergePilot pullNumber=$PR2 commit_title="rec unkused" merge_method=squash approval_ticket=$TKT2 >/dev/null 2>&1
  # merge 后 approval 可能 USED;手动改回 UNKNOWN(executing_at old)模拟"之前超时了但实际已 merge"
  PSQL "UPDATE approvals SET status='UNKNOWN', executing_at=now()-interval '200 seconds' WHERE ticket_id='$TKT2';" >/dev/null
  reconcile >/dev/null
  AST2=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT2';")
  OST2=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  TST2=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN2';")
  logf "  approval=$AST2 outbox=$OST2 task=$TST2(PR 已 merged → USED + 收敛)"
  [ "$AST2" = "USED" ] && ok "UNKNOWN + 已合并 → USED" || bad "应 USED: $AST2"
  [ "$OST2" = "SUCCEEDED" ] && ok "outbox → SUCCEEDED(收敛)" || bad "outbox 应 SUCCEEDED: $OST2"
  [ "$TST2" = "MERGED" ] && ok "task → MERGED(收敛)" || bad "task 应 MERGED: $TST2"
fi

# ─── 3. PENDING 过期 → EXPIRED + outbox FAILED + task HOLD ───
log ""; log "=== 3. PENDING 过期 → EXPIRED + HOLD ==="
RUN3=b4c4-expire-$TS
mkrun "$RUN3"; PR3=$(create_fix_pr "fix/$RUN3-x" "rec-exp-$TS.md" "expire")
if [ -z "$PR3" ]; then bad "EXPIRE: PR 创建失败"; else
  run_py "import controller; controller.discover_binding_for_run('$RUN3'); controller.create_ticket_for_run('$RUN3')" >/dev/null
  BID3=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN3';")
  TKT3=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID3';")
  # 手动设 approval_expires_at 过期(PENDING 态)
  PSQL "UPDATE approvals SET status='PENDING', approval_expires_at=now()-interval '1 hour' WHERE ticket_id='$TKT3';" >/dev/null
  reconcile >/dev/null
  AST3=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT3';")
  OST3=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT3';")
  TST3=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN3';")
  logf "  approval=$AST3 outbox=$OST3 task=$TST3"
  [ "$AST3" = "EXPIRED" ] && ok "PENDING 过期 → EXPIRED" || bad "应 EXPIRED: $AST3"
  [ "$OST3" = "FAILED" ] && ok "outbox → FAILED" || bad "outbox 应 FAILED: $OST3"
  [ "$TST3" = "HOLD" ] && ok "task → HOLD" || bad "task 应 HOLD: $TST3"
fi

# ─── 4. 滞留 DISPATCHED(approval=USED)→ reconcile → outbox SUCCEEDED ───
log ""; log "=== 4. 滞留 DISPATCHED(approval=USED)→ outbox SUCCEEDED ==="
RUN4=b4c4-stranded-$TS
mkrun "$RUN4"; PR4=$(create_fix_pr "fix/$RUN4-x" "rec-str-$TS.md" "stranded")
if [ -z "$PR4" ]; then bad "STRANDED: PR 创建失败"; else
  run_py "import controller; controller.discover_binding_for_run('$RUN4'); controller.create_ticket_for_run('$RUN4')" >/dev/null
  BID4=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN4';")
  TKT4=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID4';")
  # 模拟"Gateway 已 USED 但 outbox 仍 DISPATCHED(crash 在 advance 前)"
  PSQL "UPDATE approvals SET status='USED', used_at=now(), result_sha='fakeusedsha12345678901234567890123456789012' WHERE ticket_id='$TKT4';" >/dev/null
  PSQL "UPDATE policy_action_outbox SET status='DISPATCHED' WHERE ticket_id='$TKT4';" >/dev/null
  reconcile >/dev/null
  OST4=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT4';")
  logf "  outbox=$OST4(approval=USED,outbox 滞留 DISPATCHED → 应 SUCCEEDED)"
  [ "$OST4" = "SUCCEEDED" ] && ok "滞留 DISPATCHED(approval=USED)→ SUCCEEDED" || bad "应 SUCCEEDED: $OST4"
fi

# ─── 5. 新鲜 UNKNOWN 不对账(executing_at <120s → 不拾取,防竞态)───
log ""; log "=== 5. 新鲜 UNKNOWN(executing_at<120s)不对账 ==="
RUN5=b4c4-fresh-$TS
TKT5=$(setup_unknown_ticket "$RUN5" "fix/$RUN5-x" "rec-fresh-$TS.md" "fresh")
if [ -z "$TKT5" ]; then bad "FRESH: setup 失败"; else
  # 设 executing_at = now()(新鲜,<120s,不应被对账)
  PSQL "UPDATE approvals SET executing_at=now() WHERE ticket_id='$TKT5';" >/dev/null
  reconcile >/dev/null
  AST5=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT5';")
  logf "  approval=$AST5(新鲜 UNKNOWN → 应仍 UNKNOWN,不对账)"
  [ "$AST5" = "UNKNOWN" ] && ok "新鲜 UNKNOWN 不对账(executing_at<120s,防竞态)" || bad "新鲜 UNKNOWN 被对账了: $AST5"
fi

# ─── 6. GitHub RETRY(bad gateway)→ 状态不变 ───
log ""; log "=== 6. GitHub RETRY(bad gateway)→ 所有状态不变 ==="
RUN6=b4c4-retry-$TS
TKT6=$(setup_unknown_ticket "$RUN6" "fix/$RUN6-x" "rec-retry-$TS.md" "retry")
if [ -z "$TKT6" ]; then bad "RETRY: setup 失败"; else
  OST6_BEFORE=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT6';")
  # reconcile 经坏 GATEWAY_URL → gateway_read_pr 返回 RETRY → 不迁移
  run_py_bad(){ docker run --rm --network hiclab-net --env-file "$ENVF" -e PG_HOST=audit-pg -e PG_DATABASE=mergepilot_audit \
    -e PG_USER=mergepilot -e GATEWAY_URL="http://policy-gw-unreachable:9999" -e COORDINATOR_TOKEN="$COORD" -e L2_MERGE_ENABLED=0 -e L2_GW_TIMEOUT=5 \
    "$IMG" python3 -c "$1" 2>&1 | grep -vE "^\[ctrl\]"; }
  run_py_bad "import controller; controller.reconcile_l2()" >/dev/null
  AST6=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT6';")
  OST6=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT6';")
  logf "  approval=$AST6 outbox=$OST6(RETRY → 应不变)"
  [ "$AST6" = "UNKNOWN" ] && ok "approval 仍 UNKNOWN(RETRY 不迁移)" || bad "approval 变了: $AST6"
  [ "$OST6" = "$OST6_BEFORE" ] && ok "outbox 不变(RETRY 不收敛)" || bad "outbox 变了: $OST6_BEFORE→$OST6"
fi

# ─── 6.5. stale EXPIRED:task 已脱离 → CAS 失败 → outbox.error 含 CONCURRENT_STATE_CHANGE(B4c-4.2)───
log ""; log "=== 6.5. stale EXPIRED → outbox FAILED + CONCURRENT_STATE_CHANGE(task CAS 对称) ==="
RUN65=b4c4-expstale-$TS
mkrun "$RUN65"; PR65=$(create_fix_pr "fix/$RUN65-x" "rec-expst-$TS.md" "expstale")
if [ -z "$PR65" ]; then bad "EXP-STALE: PR 创建失败"; else
  run_py "import controller; controller.discover_binding_for_run('$RUN65'); controller.create_ticket_for_run('$RUN65')" >/dev/null
  BID65=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN65';")
  TKT65=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID65';")
  # 过期 PENDING + task 提前脱离(模拟另一流程先 HOLD)
  PSQL "UPDATE approvals SET status='PENDING', approval_expires_at=now()-interval '1 hour' WHERE ticket_id='$TKT65';" >/dev/null
  PSQL "UPDATE task_runs SET status='HOLD', current_stage='l2_awaiting_approval', last_error='另一流程先 HOLD' WHERE run_id='$RUN65';" >/dev/null
  reconcile >/dev/null
  AST65=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT65';")
  OST65=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT65';")
  TST65=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN65';")
  OERR65=$(PSQL "SELECT error FROM policy_action_outbox WHERE ticket_id='$TKT65';")
  logf "  approval=$AST65 outbox=$OST65 task=$TST65 err=${OERR65:0:80}"
  [ "$AST65" = "EXPIRED" ] && ok "approval → EXPIRED" || bad "应 EXPIRED: $AST65"
  [ "$OST65" = "FAILED" ] && ok "outbox → FAILED" || bad "outbox 应 FAILED: $OST65"
  [ "$TST65" = "HOLD" ] && ok "task 不被覆盖(仍 HOLD,CAS 失败)" || bad "task 被覆盖: $TST65"
  echo "$OERR65" | grep -qi "EXPIRED" && ok "outbox.error 含 'ticket EXPIRED'" || bad "缺 EXPIRED 标记"
  echo "$OERR65" | grep -qi "CONCURRENT_STATE_CHANGE" && ok "outbox.error 含 CONCURRENT_STATE_CHANGE(对称 CAS)" || bad "缺 CONCURRENT_STATE_CHANGE"
fi

# ─── 7. 凭证扫描 ───
log ""; log "=== 5. 凭证扫描 ==="
set +e; grep -rniE "token=[A-Za-z0-9]{8}|Bearer [A-Za-z0-9]{8}|sk-live|access_token" "$EV" > "$EV/credential-scan.txt" 2>/dev/null; GR=$?; set -e
[ "$GR" -ne 0 ] && { : > "$EV/credential-scan.txt"; ok "无凭证泄漏"; } || bad "凭证泄漏"

cleanup_runs
trap - EXIT
log ""
log "═══════════════════════════════════════════════"
log "  B4c-4 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
docker start "$NAME" >/dev/null 2>&1 || true
[ "$FAIL" -eq 0 ] || exit 1
