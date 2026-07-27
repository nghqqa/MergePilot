#!/bin/bash
# m3b-b4c2-ticket.sh — B4c-2 幂等建票验收(固化双源 SHA → canonical payload/hash → l2_ensure_ticket + 同事务 outbox → l2_awaiting_approval)。
# 停 controller loop,用 docker run --rm 一次性容器调 discover→create_ticket(避免 loop 竞争)。
# 覆盖:CREATED(approvals PENDING + outbox PENDING_DISPATCH + task l2_awaiting_approval)/
#   args_hash 契约自检(payload 独立复算 == approvals.args_hash)/ 幂等(重复建票同 ticket_id,attempt_no=1)/
#   CAS CONCURRENT(阶段已改不建票)。
set -uo pipefail
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c/2-ticket
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out 2>/dev/null || true
OUT="$EV/ticket-test.out"; : > "$OUT"
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
cleanup_runs(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4c2-%'; DELETE FROM approvals WHERE run_id LIKE 'b4c2-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4c2-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4c2-%';" >/dev/null 2>&1 || true; }
restore(){ docker start "$NAME" >/dev/null 2>&1 || true; cleanup_runs; }
trap restore EXIT
# args_hash 独立复算(与 gateway.canonical_args_hash 一致:sort_keys+紧凑,排除 approval_ticket)
chash(){ python3 -c "import hashlib,json,sys; d=json.loads(sys.argv[1]); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }

log "═══════════════════════════════════════════════"
log "  B4c-2 幂等建票验收"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker cp /mnt/d/goai/mergepilot-os/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py >/dev/null 2>&1
cleanup_runs
# 构建镜像 + 容器源码哈希(承 B4c-1.2:证明 :latest 对应当前 commit)
docker build -t "$IMG" /mnt/d/goai/mergepilot-os/tools/workflow-controller/ >>"$OUT" 2>&1 || { bad "镜像 build 失败"; exit 1; }
for f in controller.py gateway_client.py; do
  ch=$(docker run --rm "$IMG" python3 -c "import hashlib;print(hashlib.sha256(open('/app/$f','rb').read()).hexdigest()[:16])" 2>/dev/null)
  rh=$(sha256sum "/mnt/d/goai/mergepilot-os/tools/workflow-controller/$f" | cut -c1-16)
  [ "$ch" = "$rh" ] && ok "$f 容器内==仓库" || bad "$f 漂移(container=$ch repo=$rh)"
done
docker stop "$NAME" >/dev/null 2>&1 || true
log "  controller loop stopped(direct discover+create_ticket via one-shot)"

# 一次性容器调用 controller 函数:$1=func_expr(python),其余 env
run_py(){ docker run --rm --network hiclab-net --env-file "$ENVF" \
  -e PG_HOST=audit-pg -e PG_DATABASE=mergepilot_audit -e PG_USER=mergepilot \
  -e GATEWAY_URL=http://policy-gw:8083 -e COORDINATOR_TOKEN="$COORD" -e L2_MERGE_ENABLED=0 -e L2_GW_TIMEOUT=60 \
  "$IMG" python3 -c "$1" 2>&1 | grep -E "^STATUS=|^INFO="; }
discover(){ run_py "
import controller, json
s,i = controller.discover_binding_for_run('$1')
print('STATUS='+str(s)); print('INFO='+json.dumps(i, default=str))"; }
create_ticket(){ run_py "
import controller, json
s,i = controller.create_ticket_for_run('$1')
print('STATUS='+str(s)); print('INFO='+json.dumps(i, default=str))"; }
create_fix_pr(){ local BR="$1" P="$2" L="$3" R
  GW fixer --call create_branch owner=nghqqa repo=MergePilot branch="$BR" from_branch=main 2>&1 | grep -qi ref && logf "  分支 $BR 建好" || logf "  分支 $BR 可能已存在"
  GW fixer --call create_or_update_file owner=nghqqa repo=MergePilot path="$P" branch="$BR" content="b4c2-$L-$TS" message="b4c2 $L" 2>&1 | grep -qi "commit\|sha\|content" && logf "  commit 加好($P)"
  R=$(GW fixer --call create_pull_request owner=nghqqa repo=MergePilot head="$BR" base=main title="B4c-2 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1
}
mkrun(){ PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$1','APPROVAL_PENDING','nghqqa/MergePilot',0,'l2_binding',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_binding',approval_required=TRUE;" >/dev/null; }
reset_ticket_stage(){ PSQL "UPDATE task_runs SET status='APPROVAL_PENDING', current_stage='l2_awaiting_ticket' WHERE run_id='$1';" >/dev/null; }

# ─── 1. 全链:discover FOUND → create_ticket CREATED ───
log ""; log "=== 1. discover → FOUND,create_ticket → CREATED ==="
RUN1=b4c2-found-$TS; BR1=fix/$RUN1-extra
mkrun "$RUN1"
PR1=$(create_fix_pr "$BR1" "ticket-found-$TS.md" "found")
if [ -z "$PR1" ]; then bad "PR 创建失败"; exit 1; fi
D1=$(discover "$RUN1")
echo "$D1" | grep -q "^STATUS=FOUND" && ok "discover → FOUND(binding + l2_awaiting_ticket)" || { bad "discover 未 FOUND: $(echo "$D1"|head -1)"; exit 1; }
BID1=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN1';")
[ -n "$BID1" ] && ok "binding 已建($BID1)" || bad "binding 未建"
T1=$(create_ticket "$RUN1")
logf "  create_ticket: $(echo "$T1" | grep '^STATUS=' | head -1)"
echo "$T1" | grep -q "^STATUS=CREATED" && ok "create_ticket → CREATED" || bad "应 CREATED: $(echo "$T1"|head -1)"
TKT1=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID1';")
AST=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
OST=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT1';")
TST=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN1';")
ATT=$(PSQL "SELECT attempt_no FROM approvals WHERE ticket_id='$TKT1';")
[ -n "$TKT1" ] && ok "approvals 票已建($TKT1)" || bad "票未建"
[ "$AST" = "PENDING" ] && ok "approvals.status=PENDING" || bad "票状态异常: $AST"
[ "$OST" = "PENDING_DISPATCH" ] && ok "outbox.status=PENDING_DISPATCH(同事务)" || bad "outbox 异常: $OST"
[ "$TST" = "l2_awaiting_approval" ] && ok "task → l2_awaiting_approval(推进)" || bad "task stage 异常: $TST"
[ "$ATT" = "1" ] && ok "attempt_no=1(首建)" || bad "attempt 异常: $ATT"

# ─── 2. args_hash 契约自检(payload 独立复算 == approvals.args_hash)───
log ""; log "=== 2. args_hash 契约(payload 复算 == approvals.args_hash) ==="
PAYLOAD="{\"owner\":\"nghqqa\",\"repo\":\"MergePilot\",\"pullNumber\":$PR1,\"commit_title\":\"Merge fix $RUN1\",\"merge_method\":\"squash\"}"
RECOMP=$(chash "$PAYLOAD")
STORED=$(PSQL "SELECT args_hash FROM approvals WHERE ticket_id='$TKT1';")
logf "  recompute=$RECOMP"
logf "  approvals.args_hash=$STORED"
[ "$RECOMP" = "$STORED" ] && ok "args_hash 契约一致(建票 hash == 独立复算;Gateway drain 时 canonical_args_hash 同源)" || bad "args_hash 失配: recompute=$RECOMP stored=$STORED"
[ "${#STORED}" = "64" ] && ok "args_hash 完整 64hex" || bad "args_hash 非 64hex: len=${#STORED}"

# ─── 3. 幂等:重复 create_ticket → 同 ticket_id,attempt_no 不增 ───
log ""; log "=== 3. 幂等:重复建票 → 同票 ==="
reset_ticket_stage "$RUN1"
T2=$(create_ticket "$RUN1")
TKT2=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID1';")
ATT2=$(PSQL "SELECT attempt_no FROM approvals WHERE ticket_id='$TKT1';")
ACNT=$(PSQL "SELECT count(*) FROM approvals WHERE binding_id='$BID1' AND action='merge';")
OCNT=$(PSQL "SELECT count(*) FROM policy_action_outbox WHERE ticket_id='$TKT1';")
echo "$T2" | grep -q "^STATUS=CREATED" && ok "重复 create_ticket → CREATED(l2_ensure_ticket 返回活动票)" || bad "重复应 CREATED: $(echo "$T2"|head -1)"
[ "$TKT1" = "$TKT2" ] && ok "同 ticket_id(幂等,不新建)" || bad "ticket 变了: $TKT1→$TKT2"
[ "$ATT2" = "1" ] && ok "attempt_no 仍 1(未新建 attempt)" || bad "attempt 增了: $ATT2"
[ "$ACNT" = "1" ] && ok "approvals 仍 1 行(无重复)" || bad "approvals 多行: $ACNT"
[ "$OCNT" = "1" ] && ok "outbox 仍 1 行(无重复派发)" || bad "outbox 多行: $OCNT"

# ─── 4. CAS CONCURRENT:阶段已改 → 不建票 ───
log ""; log "=== 4. CAS CONCURRENT(阶段已改不建票) ==="
# 模拟另一 Controller 已推进到 l2_awaiting_approval
PSQL "UPDATE task_runs SET current_stage='l2_awaiting_approval' WHERE run_id='$RUN1';" >/dev/null
ACNT_BEFORE=$(PSQL "SELECT count(*) FROM approvals WHERE binding_id='$BID1';")
T3=$(create_ticket "$RUN1")
ACNT_AFTER=$(PSQL "SELECT count(*) FROM approvals WHERE binding_id='$BID1';")
echo "$T3" | grep -q "^STATUS=CONCURRENT" && ok "阶段已改 → CONCURRENT" || bad "应 CONCURRENT: $(echo "$T3"|head -1)"
[ "$ACNT_AFTER" = "$ACNT_BEFORE" ] && ok "未新建票(CAS 失败不写)" || bad "却新建了票: $ACNT_BEFORE→$ACNT_AFTER"

# ─── 5. HOLD_NO_BINDING:删 binding → 原子置 HOLD(无票无 outbox)───
log ""; log "=== 5. HOLD_NO_BINDING:无 binding → task HOLD(不每 tick 重复) ==="
RUN5=b4c2-nobinding-$TS
PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN5','APPROVAL_PENDING','nghqqa/MergePilot',0,'l2_awaiting_ticket',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_ticket';" >/dev/null
# 不建 binding(也不建 PR)→ create_ticket 应 HOLD_NO_BINDING
T5=$(create_ticket "$RUN5")
ST5=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN5';")
CS5=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN5';")
AC5=$(PSQL "SELECT count(*) FROM approvals WHERE run_id='$RUN5';")
OC5=$(PSQL "SELECT count(*) FROM policy_action_outbox WHERE run_id='$RUN5';")
logf "  create_ticket: $(echo "$T5" | grep '^STATUS=' | head -1)"
echo "$T5" | grep -q "^STATUS=HOLD_NO_BINDING" && ok "无 binding → HOLD_NO_BINDING" || bad "应 HOLD_NO_BINDING: $(echo "$T5"|head -1)"
[ "$ST5" = "HOLD" ] && ok "DB status=HOLD(原子置)" || bad "未 HOLD: $ST5"
[ "$CS5" = "l2_ticket_failed" ] && ok "current_stage=l2_ticket_failed" || bad "stage 异常: $CS5"
[ "$AC5" = "0" ] && ok "无票" || bad "却建了票: $AC5"
[ "$OC5" = "0" ] && ok "无 outbox" || bad "却写了 outbox: $OC5"

# ─── 6. HOLD_TICKET_CONFLICT:旧票 args_hash 不一致 → 22023 → HOLD(旧票不变,无第二张)───
log ""; log "=== 6. HOLD_TICKET_CONFLICT:旧票 payload/hash 不一致 → 22023 → HOLD ==="
# 复用 RUN1/BID1/TKT1(已 CREATED):reset stage + corrupt 旧票 args_hash,再建票
PSQL "UPDATE task_runs SET status='APPROVAL_PENDING', current_stage='l2_awaiting_ticket' WHERE run_id='$RUN1';" >/dev/null
PSQL "UPDATE approvals SET args_hash='deadbeef' || substring(args_hash,9) WHERE ticket_id='$TKT1';" >/dev/null   # 破坏 args_hash 前 8 字符(确定改变)
CORRUPT_HASH=$(PSQL "SELECT args_hash FROM approvals WHERE ticket_id='$TKT1';")
logf "  旧票 $TKT1 args_hash 已 corrupt: ${CORRUPT_HASH:0:12}..."
T6=$(create_ticket "$RUN1")
ST6=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN1';")
CS6=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN1';")
AC6=$(PSQL "SELECT count(*) FROM approvals WHERE binding_id='$BID1' AND action='merge';")
OLDST=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
logf "  create_ticket: $(echo "$T6" | grep '^STATUS=' | head -1)"
echo "$T6" | grep -q "^STATUS=HOLD_TICKET_CONFLICT" && ok "旧票 hash 不一致 → HOLD_TICKET_CONFLICT(22023,不重试)" || bad "应 HOLD_TICKET_CONFLICT: $(echo "$T6"|head -1)"
[ "$ST6" = "HOLD" ] && ok "DB status=HOLD" || bad "未 HOLD: $ST6"
[ "$CS6" = "l2_ticket_failed" ] && ok "current_stage=l2_ticket_failed" || bad "stage 异常: $CS6"
[ "$AC6" = "1" ] && ok "无第二张票(仍 1 张)" || bad "建了第二张: $AC6"
[ "$OLDST" = "PENDING" ] && ok "旧票不变(仍 PENDING,未被修改)" || bad "旧票被改: $OLDST"

# ─── 7. PG-wait 行为单元测试(monkeypatch:连接成功但 SELECT 1 持续抛错)───
log ""; log "=== 7. PG-wait 行为单元(_wait_for_pg + startup SystemExit) ==="
docker run --rm "$IMG" python3 /app/test_startup_pgwait.py > "$EV/pgwait-unit.out" 2>&1
tail -1 "$EV/pgwait-unit.out" | tee -a "$OUT"
grep -q "FAIL=0" "$EV/pgwait-unit.out" && ok "PG-wait 单元 PASS(30 次重试/ready=False/conn=None/第5次成功/startup SystemExit)" || { bad "PG-wait 单元 FAIL"; tail -10 "$EV/pgwait-unit.out" | tee -a "$OUT"; }

# ─── 8. 凭证扫描 ───
log ""; log "=== 5. 凭证扫描 ==="
set +e; grep -rniE "token=[A-Za-z0-9]{8}|Bearer [A-Za-z0-9]{8}|sk-live|access_token" "$EV" > "$EV/credential-scan.txt" 2>/dev/null; GR=$?; set -e
[ "$GR" -ne 0 ] && { : > "$EV/credential-scan.txt"; ok "无凭证泄漏"; } || bad "凭证泄漏"

PSQL "SELECT t.ticket_id,t.status,t.attempt_no,t.args_hash,o.status as outbox FROM approvals t LEFT JOIN policy_action_outbox o ON t.ticket_id=o.ticket_id WHERE t.run_id LIKE 'b4c2-%';" > "$EV/tickets-snapshot.txt" 2>/dev/null
cleanup_runs
trap - EXIT
log ""
log "═══════════════════════════════════════════════"
log "  B4c-2 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
docker start "$NAME" >/dev/null 2>&1 || true
[ "$FAIL" -eq 0 ] || exit 1
