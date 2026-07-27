#!/bin/bash
# m3b-b4c3-drain.sh — B4c-3 lease drain 验收(守三边界:领取事务提交后调 Gateway / 读 approvals 权威态 / UNKNOWN·EXECUTING 不重 merge)。
# 覆盖:USED→MERGED(真 merge)/ FAILED→HOLD(fault upstream_error)/ UNKNOWN 不重派(fault write_timeout)/
#   lease 写入 / crash 后滞留 + lease 过期重派(attempts +1,每次真实派发计数)/ 并发领取 SKIP LOCKED(只胜者 +1)。
set -uo pipefail
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c/3-drain
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out 2>/dev/null || true
OUT="$EV/drain-test.out"; : > "$OUT"
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
TS=$$
ENVF="$DIR/controller.env"
cleanup_runs(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4c3-%'; DELETE FROM approvals WHERE run_id LIKE 'b4c3-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4c3-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4c3-%';" >/dev/null 2>&1 || true; }
restore(){ docker rm -f policy-gw-fault-fail policy-gw-fault-unk 2>/dev/null; docker start "$NAME" >/dev/null 2>&1 || true; cleanup_runs; }
trap restore EXIT

log "═══════════════════════════════════════════════"
log "  B4c-3 lease drain 验收"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker cp /mnt/d/goai/mergepilot-os/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py >/dev/null 2>&1
# 构建镜像 + 容器哈希
docker build -t "$IMG" /mnt/d/goai/mergepilot-os/tools/workflow-controller/ >>"$OUT" 2>&1 || { bad "镜像 build 失败"; exit 1; }
for f in controller.py gateway_client.py; do
  ch=$(docker run --rm "$IMG" python3 -c "import hashlib;print(hashlib.sha256(open('/app/$f','rb').read()).hexdigest()[:16])" 2>/dev/null)
  rh=$(sha256sum "/mnt/d/goai/mergepilot-os/tools/workflow-controller/$f" | cut -c1-16)
  [ "$ch" = "$rh" ] && ok "$f 容器内==仓库" || bad "$f 漂移"
done
# fault gateways(FAILED/UNKNOWN)
touch /tmp/.test-mode
fault_gw(){ docker rm -f "policy-gw-$2" 2>/dev/null; docker run -d --name "policy-gw-$2" --network hiclab-net --restart no \
  -v /tmp/.test-mode:/tmp/.test_mode -e ROLE_TOKENS="$ROLE_TOKENS_VAL" -e UPSTREAM_URL="http://github-mcp:8082/sse" \
  -e AUDIT_DSN="$AUDIT_DSN_VAL" -e L2_DSN="$L2_DSN_VAL" -e L2_TIMEOUT_SECONDS=2 -e FAULT_INJECT="$1" policy-gateway:latest >/dev/null 2>&1
  docker network connect mcp-backend-net "policy-gw-$2" 2>/dev/null
  for i in $(seq 1 15); do docker logs "policy-gw-$2" 2>&1 | grep -qa "upstream ready" && break; sleep 1; done; }
fault_gw upstream_error fault-fail
fault_gw write_timeout fault-unk
docker stop "$NAME" >/dev/null 2>&1 || true
cleanup_runs

# 一次性容器调 controller: $1=python_expr, $2=GATEWAY_URL(默认真实)
run_py(){ local GWU="${2:-http://policy-gw:8083}"
  docker run --rm --network hiclab-net --env-file "$ENVF" -e PG_HOST=audit-pg -e PG_DATABASE=mergepilot_audit \
    -e PG_USER=mergepilot -e GATEWAY_URL="$GWU" -e COORDINATOR_TOKEN="$COORD" -e L2_MERGE_ENABLED=0 -e L2_GW_TIMEOUT=15 \
    "$IMG" python3 -c "$1" 2>&1 | grep -E "^STATUS=|^INFO="; }
discover(){ run_py "import controller,json; s,i=controller.discover_binding_for_run('$1'); print('STATUS='+str(s)); print('INFO='+json.dumps(i,default=str))"; }
create_ticket(){ run_py "import controller,json; s,i=controller.create_ticket_for_run('$1'); print('STATUS='+str(s)); print('INFO='+json.dumps(i,default=str))"; }
# drain:$1=GATEWAY_URL
drain(){ run_py "import controller; controller.drain_l2_outbox()" "${1:-http://policy-gw:8083}"; }
create_fix_pr(){ local BR="$1" P="$2" L="$3" R
  GW fixer --call create_branch owner=nghqqa repo=MergePilot branch="$BR" from_branch=main 2>&1 | grep -qi ref && logf "  分支 $BR 建好"
  GW fixer --call create_or_update_file owner=nghqqa repo=MergePilot path="$P" branch="$BR" content="b4c3-$L-$TS" message="b4c3 $L" 2>&1 | grep -qi "commit\|sha" && logf "  commit 加好"
  R=$(GW fixer --call create_pull_request owner=nghqqa repo=MergePilot head="$BR" base=main title="B4c-3 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
mkrun(){ PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$1','APPROVAL_PENDING','nghqqa/MergePilot',0,'l2_binding',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_binding',approval_required=TRUE;" >/dev/null; }
# 全链到 APPROVED:discover→ticket→approve。返回 ticket_id
setup_approved(){ local RUN="$1" BR="$2" P="$3" L="$4" PR TKT BID
  mkrun "$RUN"; PR=$(create_fix_pr "$BR" "$P" "$L")
  [ -z "$PR" ] && { echo ""; return; }
  discover "$RUN" >/dev/null; create_ticket "$RUN" >/dev/null
  BID=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN';")
  TKT=$(PSQL "SELECT ticket_id FROM approvals WHERE binding_id='$BID';")
  PSQL "SELECT l2_approve('$TKT','b4c3-test@host');" >/dev/null
  echo "$TKT"; }

# ─── 1. USED → MERGED(真 gateway,真 merge)───
log ""; log "=== 1. USED → MERGED(真 merge) ==="
RUN1=b4c3-used-$TS
TKT1=$(setup_approved "$RUN1" "fix/$RUN1-x" "drain-used-$TS.md" "used")
if [ -z "$TKT1" ]; then bad "USED: setup 失败"; else
  AST_BEFORE=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  [ "$AST_BEFORE" = "APPROVED" ] && ok "票 APPROVED(待 drain)" || bad "票未 APPROVED: $AST_BEFORE"
  drain http://policy-gw:8083 >/dev/null
  AST1=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  OST1=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT1';")
  TST1=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN1';")
  SHA1=$(PSQL "SELECT result_sha FROM approvals WHERE ticket_id='$TKT1';")
  logf "  approval=$AST1 outbox=$OST1 task=$TST1 sha=${SHA1:0:12}"
  [ "$AST1" = "USED" ] && ok "approval → USED" || bad "approval 应 USED: $AST1"
  [ "$OST1" = "SUCCEEDED" ] && ok "outbox → SUCCEEDED" || bad "outbox 应 SUCCEEDED: $OST1"
  [ "$TST1" = "MERGED" ] && ok "task → MERGED" || bad "task 应 MERGED: $TST1"
  [ -n "$SHA1" ] && [ "$SHA1" != "" ] && ok "result_sha 记录(merge commit)" || bad "result_sha 空"
fi

# ─── 2. FAILED → HOLD(fault upstream_error)───
log ""; log "=== 2. FAILED → HOLD(fault gateway upstream_error) ==="
RUN2=b4c3-failed-$TS
TKT2=$(setup_approved "$RUN2" "fix/$RUN2-x" "drain-fail-$TS.md" "failed")
if [ -z "$TKT2" ]; then bad "FAILED: setup 失败"; else
  drain http://policy-gw-fault-fail:8083 >/dev/null
  AST2=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT2';")
  OST2=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT2';")
  TST2=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN2';")
  logf "  approval=$AST2 outbox=$OST2 task=$TST2"
  [ "$AST2" = "FAILED" ] && ok "approval → FAILED(fault upstream_error)" || bad "应 FAILED: $AST2"
  [ "$OST2" = "FAILED" ] && ok "outbox → FAILED" || bad "outbox 应 FAILED: $OST2"
  [ "$TST2" = "HOLD" ] && ok "task → HOLD" || bad "task 应 HOLD: $TST2"
fi

# ─── 3. UNKNOWN → 不重派(fault write_timeout)───
log ""; log "=== 3. UNKNOWN → outbox UNKNOWN,不重派(交 B4c-4) ==="
RUN3=b4c3-unknown-$TS
TKT3=$(setup_approved "$RUN3" "fix/$RUN3-x" "drain-unk-$TS.md" "unknown")
if [ -z "$TKT3" ]; then bad "UNKNOWN: setup 失败"; else
  drain http://policy-gw-fault-unk:8083 >/dev/null
  AST3=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT3';")
  OST3=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT3';")
  TST3=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN3';")
  ATT3=$(PSQL "SELECT attempts FROM policy_action_outbox WHERE ticket_id='$TKT3';")
  logf "  approval=$AST3 outbox=$OST3 task=$TST3 attempts=$ATT3"
  [ "$AST3" = "UNKNOWN" ] && ok "approval → UNKNOWN(write_timeout)" || bad "应 UNKNOWN: $AST3"
  [ "$OST3" = "UNKNOWN" ] && ok "outbox → UNKNOWN(不重派,交 B4c-4 对账)" || bad "outbox 应 UNKNOWN: $OST3"
  [ "$TST3" = "APPROVAL_PENDING" ] && ok "task 留 APPROVAL_PENDING(不 HOLD,等对账)" || bad "task 异常: $TST3"
  # 再 drain 一次(真 gateway)→ 不应重 merge(outbox 已 UNKNOWN,不在 PENDING_DISPATCH)
  drain http://policy-gw:8083 >/dev/null
  AST3b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT3';")
  [ "$AST3b" = "UNKNOWN" ] && ok "UNKNOWN 不被重 merge(再 drain 仍 UNKNOWN)" || bad "UNKNOWN 被重派了: $AST3b"
fi

# ─── 4. lease 写入 + crash 后滞留(bad GATEWAY_URL → Gateway 不可达 → approval 仍 APPROVED → outbox DISPATCHED 滞留)───
log ""; log "=== 4. lease 写入 + crash 后滞留(bad gateway) ==="
RUN4=b4c3-crash-$TS
TKT4=$(setup_approved "$RUN4" "fix/$RUN4-x" "drain-crash-$TS.md" "crash")
if [ -z "$TKT4" ]; then bad "CRASH: setup 失败"; else
  drain http://policy-gw-unreachable:9999 >/dev/null   # 领取(DISPATCHED+lease,提交)→ Gateway 不可达 → 读 approval=APPROVED → outbox 留 DISPATCHED
  OST4=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT4';")
  LEASE4=$(PSQL "SELECT lease_expires_at IS NOT NULL FROM policy_action_outbox WHERE ticket_id='$TKT4';")
  AST4=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT4';")
  ATT4=$(PSQL "SELECT attempts FROM policy_action_outbox WHERE ticket_id='$TKT4';")
  logf "  outbox=$OST4 lease_set=$LEASE4 approval=$AST4 attempts=$ATT4"
  [ "$LEASE4" = "t" ] && ok "lease_expires_at 已写入(领取时)" || bad "lease 未写"
  [ "$OST4" = "DISPATCHED" ] && ok "outbox 留 DISPATCHED(crash 后滞留,等 lease 恢复)" || bad "outbox 异常: $OST4"
  [ "$AST4" = "APPROVED" ] && ok "approval 仍 APPROVED(Gateway 未 claim,未发生 merge)" || bad "approval 异常: $AST4"
  [ "$ATT4" = "1" ] && ok "attempts=1(首次领取)" || bad "attempts 异常: $ATT4"
  # 模拟 lease 过期 → drain(真 gateway)安全重派;B4c-3.1:每次真实派发 attempts +1 → 1→2
  PSQL "UPDATE policy_action_outbox SET lease_expires_at = now() - interval '1 minute' WHERE ticket_id='$TKT4';" >/dev/null
  drain http://policy-gw:8083 >/dev/null
  OST4b=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT4';")
  AST4b=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT4';")
  ATT4b=$(PSQL "SELECT attempts FROM policy_action_outbox WHERE ticket_id='$TKT4';")
  logf "  lease 过期重派后: outbox=$OST4b approval=$AST4b attempts=$ATT4b"
  [ "$ATT4b" = "2" ] && ok "lease 重派 +1 → attempts 1→2(每次真实派发计数,审计可区分派发次数)" || bad "lease 重派 attempts 异常: $ATT4b(应 2)"
fi

# ─── 5. 并发 drain:SKIP LOCKED → 只一个领取 ───
log ""; log "=== 5. 并发 drain(SKIP LOCKED,只一个领取) ==="
RUN5=b4c3-conc-$TS
TKT5=$(setup_approved "$RUN5" "fix/$RUN5-x" "drain-conc-$TS.md" "conc")
if [ -z "$TKT5" ]; then bad "CONCURRENT: setup 失败"; else
  # 两个并发 drain(bad gateway,不真 merge,只测领取互斥)
  drain http://policy-gw-unreachable:9999 > /tmp/conc_a.out 2>&1 &
  drain http://policy-gw-unreachable:9999 > /tmp/conc_b.out 2>&1 &
  wait
  ATT5=$(PSQL "SELECT attempts FROM policy_action_outbox WHERE ticket_id='$TKT5';")
  OST5=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT5';")
  logf "  并发后: outbox=$OST5 attempts=$ATT5"
  [ "$ATT5" = "1" ] && ok "并发领取只 +1(SKIP LOCKED 互斥)" || bad "并发加了多次: $ATT5"
  [ "$OST5" = "DISPATCHED" ] && ok "outbox DISPATCHED(领一次)" || bad "outbox 异常: $OST5"
fi

# ─── 6. close USED → HOLD/verified-closed(action-aware,B4c-3.1 P1-1)───
log ""; log "=== 6. close USED → HOLD/verified-closed(非 MERGED) ==="
RUN6=b4c3-close-$TS; BR6=fix/$RUN6-close
mkrun "$RUN6"
PR6=$(create_fix_pr "$BR6" "drain-close-$TS.md" "close")
if [ -z "$PR6" ]; then bad "close: PR 创建失败"; else
  discover "$RUN6" >/dev/null
  BID6=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN6';")
  # 手动建 close 票(action='close',payload 含 state=closed)
  CPAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":'$PR6',"state":"closed"}'
  CAH=$(python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$CPAYLOAD")
  TKT6=$(PSQL "SELECT l2_create_ticket('$BID6','close','$CPAYLOAD'::jsonb,'$CAH',24,1);")
  PSQL "SELECT l2_approve('$TKT6','b4c3-close@host');" >/dev/null
  # 手动 l2_create_ticket 不推进 task(那是 create_ticket_for_run 的活);补推进到 l2_awaiting_approval(drain 就绪态)
  PSQL "UPDATE task_runs SET status='APPROVAL_PENDING', current_stage='l2_awaiting_approval' WHERE run_id='$RUN6';" >/dev/null
  drain http://policy-gw:8083 >/dev/null
  AST6=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT6';")
  TST6=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN6';")
  CS6=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN6';")
  logf "  approval=$AST6 task=$TST6 stage=$CS6"
  [ "$AST6" = "USED" ] && ok "close → approval USED" || bad "close 应 USED: $AST6"
  [ "$TST6" = "HOLD" ] && ok "close USED → task HOLD(非 MERGED)" || bad "close task 应 HOLD: $TST6"
  [ "$CS6" = "verified-closed" ] && ok "close USED → current_stage=verified-closed" || bad "close stage 异常: $CS6"
fi

# ─── 7. stale callback:task 已脱离 → CONCURRENT_STATE_CHANGE 不覆盖(B4c-3.1 P1-2)───
log ""; log "=== 7. stale callback → CONCURRENT_STATE_CHANGE(task CAS 失败不覆盖) ==="
RUN7=b4c3-stale-$TS
TKT7=$(setup_approved "$RUN7" "fix/$RUN7-x" "drain-stale-$TS.md" "stale")
if [ -z "$TKT7" ]; then bad "STALE: setup 失败"; else
  # 模拟另一流程已把 task 推到 HOLD(脱离 APPROVAL_PENDING),drain 回调不应覆盖
  PSQL "UPDATE task_runs SET status='HOLD', current_stage='l2_awaiting_approval', last_error='另一流程先 HOLD' WHERE run_id='$RUN7';" >/dev/null
  drain http://policy-gw:8083 >/dev/null
  TST7=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN7';")
  CS7=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN7';")
  OERR7=$(PSQL "SELECT error FROM policy_action_outbox WHERE ticket_id='$TKT7';")
  logf "  task=$TST7 stage=$CS7 outbox_err=${OERR7:0:50}"
  [ "$TST7" = "HOLD" ] && ok "task 不被覆盖(仍 HOLD,未变 MERGED)" || bad "task 被覆盖: $TST7"
  echo "$OERR7" | grep -qi "CONCURRENT_STATE_CHANGE" && ok "outbox 记 CONCURRENT_STATE_CHANGE(CAS 失败可见)" || bad "缺 CONCURRENT_STATE_CHANGE 标记"
fi

# ─── 8. FAILED stale 对称:task 已脱离 → outbox=FAILED + error 含 CONCURRENT_STATE_CHANGE(B4c-3.2)───
log ""; log "=== 8. FAILED stale → outbox FAILED + CONCURRENT_STATE_CHANGE(对称 CAS) ==="
RUN8=b4c3-failstale-$TS
TKT8=$(setup_approved "$RUN8" "fix/$RUN8-x" "drain-failstale-$TS.md" "failstale")
if [ -z "$TKT8" ]; then bad "FAIL-STALE: setup 失败"; else
  # task 提前脱离 APPROVAL_PENDING(模拟另一流程先 HOLD);fault upstream_error → approval=FAILED
  PSQL "UPDATE task_runs SET status='HOLD', current_stage='l2_awaiting_approval', last_error='另一流程先 HOLD' WHERE run_id='$RUN8';" >/dev/null
  drain http://policy-gw-fault-fail:8083 >/dev/null
  TST8=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN8';")
  OST8=$(PSQL "SELECT status FROM policy_action_outbox WHERE ticket_id='$TKT8';")
  OERR8=$(PSQL "SELECT error FROM policy_action_outbox WHERE ticket_id='$TKT8';")
  logf "  task=$TST8 outbox=$OST8 err=${OERR8:0:80}"
  [ "$TST8" = "HOLD" ] && ok "task 不被覆盖(仍 HOLD)" || bad "task 被覆盖: $TST8"
  [ "$OST8" = "FAILED" ] && ok "outbox=FAILED(上游失败已记)" || bad "outbox 应 FAILED: $OST8"
  echo "$OERR8" | grep -qi "CONCURRENT_STATE_CHANGE" && ok "outbox.error 含 CONCURRENT_STATE_CHANGE(CAS 失败可见,对称于 USED)" || bad "缺 CONCURRENT_STATE_CHANGE"
  echo "$OERR8" | grep -qi "upstream\|FAILED\|is_error" && ok "outbox.error 同时含上游失败原因" || bad "缺上游失败原因"
fi

# ─── 9. 凭证扫描 ───
log ""; log "=== 6. 凭证扫描 ==="
set +e; grep -rniE "token=[A-Za-z0-9]{8}|Bearer [A-Za-z0-9]{8}|sk-live|access_token" "$EV" > "$EV/credential-scan.txt" 2>/dev/null; GR=$?; set -e
[ "$GR" -ne 0 ] && { : > "$EV/credential-scan.txt"; ok "无凭证泄漏"; } || bad "凭证泄漏"

PSQL "SELECT t.ticket_id,t.status,o.status,o.attempts FROM approvals t JOIN policy_action_outbox o ON t.ticket_id=o.ticket_id WHERE t.run_id LIKE 'b4c3-%' ORDER BY t.run_id;" > "$EV/drain-snapshot.txt" 2>/dev/null
cleanup_runs
trap - EXIT
log ""
log "═══════════════════════════════════════════════"
log "  B4c-3 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
docker start "$NAME" >/dev/null 2>&1 || true
[ "$FAIL" -eq 0 ] || exit 1
