#!/bin/bash
# m3c-e2e.sh — M3-C 状态感知失败处理 + 回滚 验收(fixture 隔离;child-run 模型)。
# 覆盖(需求 3/6):1.重复 TASK_COMPLETED→幂等 2.未合并 FAIL→Fixer 重试 3.未合并超 MAX→HOLD 零 revert
#   4.POST_MERGE_VERIFY_FAILED 伪造 result_sha→拒(真实入口 process_event) 5.合法→rollback PENDING + child run
#   6/7/8.已合并 FAIL→revert child run→approve→merge→REVERTED→reverify PASS→RECOVERED(container 跑真 loop)
#   9.reverify FAIL→HOLD + 不二回滚(幂等:重复 PMF 不新增) 10a.drain 跳过 PENDING 10b.显式 Gateway→CLAIM_MISMATCH
# 硬门 [FAIL=0] && [PASS=EXPECTED_PASS];fixture 0 PR/仅 main;容器日志正确落盘;无凭据泄漏。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3c
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out "$EV"/*.log 2>/dev/null || true
OUT="$EV/m3c-test.out"; : > "$OUT"
log(){ echo "$*" | tee -a "$OUT"; }
logf(){ echo "$*" >> "$OUT"; }
ok(){ log "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ log "  ❌ $1"; FAIL=$((FAIL+1)); }
PASS=0; FAIL=0; TS=$$
EXPECTED_PASS=33

CTRL=/home/ngh/.config/mergepilot/controller.env
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=mergepilot_audit
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' /home/ngh/.config/mergepilot/b4-roles.env | head -1 | cut -d= -f2-)
ECOORD=$(e2e_coordinator_token)
SERVER="matrix-local.hiclaw.io:18080"
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
ah(){ python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]),sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }
GW(){ e2e_GW "$@" 2>&1 || true; }
has(){ echo "$1" | grep -qiE "$2"; }
NL=$'\n'
ROOM="!m3c-$TS:$SERVER"
# controller 一次性容器(决策 3:MAX_VERIFY_ATTEMPTS=3;经 process_event 真实入口,不直接 INSERT stage_events)
DRUN(){ local PY="$1"; docker run --rm --network hiclab-net --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER="$PG_SU" \
  -e MATRIX_HS=http://hiclaw-controller:6167 -e ADMIN_PW="$(grep '^ADMIN_PW=' "$CTRL"|cut -d= -f2-)" -e SERVER_NAME="$SERVER" \
  -e GATEWAY_URL=http://policy-gw-e2e:8083 -e COORDINATOR_TOKEN="$ECOORD" -e L2_MERGE_ENABLED=1 -e MAX_VERIFY_ATTEMPTS=3 -e L2_GW_TIMEOUT=15 \
  mergepilot-controller:latest python3 -c "$PY" >>"$EV/run-raw.log" 2>&1; }
# 长驻 controller 容器(跑 run_forever 真 loop;用于回滚全链)
start_ctrl(){ local NM="$1"; docker rm -f "$NM" >/dev/null 2>&1 || true; docker run -d --name "$NM" --network hiclab-net --restart no --env-file "$CTRL" -e PG_HOST=audit-pg -e PG_DATABASE=$PG_DB -e PG_USER="$PG_SU" \
  -e MATRIX_HS=http://hiclaw-controller:6167 -e ADMIN_PW="$(grep '^ADMIN_PW=' "$CTRL"|cut -d= -f2-)" -e SERVER_NAME="$SERVER" \
  -e GATEWAY_URL=http://policy-gw-e2e:8083 -e COORDINATOR_TOKEN="$ECOORD" -e L2_MERGE_ENABLED=1 -e MAX_VERIFY_ATTEMPTS=3 -e POLL_INTERVAL=2 -e L2_GW_TIMEOUT=15 \
  mergepilot-controller:latest >/dev/null 2>&1; }
# 需求 6:容器日志保存 —— **先 docker logs 落盘,再 docker rm -f**(WIP 顺序错致 0 字节)
save_logs(){ docker logs "$1" >> "$EV/controller-logs.txt" 2>&1 || true; docker rm -f "$1" >/dev/null 2>&1 || true; }
# Agent 链注入(经 process_event,真实 Matrix 入口)
inject_complete(){ local STAGE="$1" SENDER="$2" RUN="$3" EVT="$4" VERDICT="${5:-}"
  local B="TASK_COMPLETED: $RUN-$STAGE"; [ -n "$VERDICT" ] && B="$B$NL$VERDICT"
  DRUN "import controller
controller.process_event('$EVT','$ROOM','$SENDER','''$B''',None)"; }
# 需求 3:POST_MERGE_VERIFY_FAILED **真实入口** —— 经 process_event(verifier sender),校验 room/run/repo/pr/result_sha。
#   绝不直接 INSERT stage_events(旧 inject_post_merge_fail 已删)。
inject_pmf(){ local RUN="$1" REPO="$2" PR="$3" SHA="$4" EVT="$5"
  local PAY; PAY=$(python3 -c "import json;print(json.dumps({'type':'POST_MERGE_VERIFY_FAILED','run_id':'$RUN','repo':'$REPO','pr_number':$PR,'result_sha':'$SHA','room':'$ROOM'}))")
  DRUN "import controller
controller.process_event('$EVT','$ROOM','verifier','''POST_MERGE_VERIFY_FAILED: $PAY''',None)"; }
# child run 命名(必须与 controller 一致: <parent>-revert-<badsha8>)→ revert 分支 fix/<child>-x
child_run_for(){ echo "$1-revert-${2:0:8}"; }
create_fix_pr_mod(){ local BR="$1" PATH_="$2" CONTENT="$3" MSG="$4" R
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" >/dev/null 2>&1
  gw_put_file "$BR" "$PATH_" "$CONTENT" "$MSG"
  R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="$MSG" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
gw_put_file(){ local BR="$1" P="$2" C="$3" M="$4" SHA
  SHA=$(gh.exe api "repos/$(e2e_repo)/contents/$P?ref=$BR" --jq '.sha' 2>/dev/null | tr -d '\000')
  e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="$P" branch="$BR" content="$C" message="$M" sha="$SHA" >/dev/null 2>&1; }
baseline_file(){ local PATH_="$1" CONTENT="$2" B64
  B64=$(python3 -c "import base64;print(base64.b64encode('$CONTENT'.encode()).decode())")
  gh.exe api -X PUT "repos/$(e2e_repo)/contents/$PATH_" -f message="m3c baseline" -f content="$B64" -f branch="$E2E_BASE_BRANCH" >/dev/null 2>&1 || true; }
read_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null; }
# 共用:建 bad fix PR(squash merge)→ MERGED + result_sha(=坏 merge)。设全局 SETUP_PR/SETUP_SHA/SETUP_TKT
setup_bad_merge(){ local RUN="$1" BFILE="$2" CLEAN="$3" BADV="$4" BR PR HS BID PAY AH TKT
  baseline_file "$BFILE" "$CLEAN"
  BR="fix/$RUN-x"; PR=$(create_fix_pr_mod "$BR" "$BFILE" "$BADV" "m3c bad fix $RUN")
  HS=$(read_sha "$PR"); BID="bnd-$RUN"
  PSQL "INSERT INTO task_runs(run_id,room_id,repo,pr_number,status,current_stage,approval_required) VALUES('$RUN','$ROOM','$(e2e_repo)',$PR,'APPROVAL_PENDING','l2_awaiting_approval',TRUE) ON CONFLICT DO NOTHING;" >/dev/null
  PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','main','$HS') ON CONFLICT(binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  PAY='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR',"commit_title":"m3c bad merge","merge_method":"squash"}'
  AH=$(ah "$PAY")
  TKT=$(PSQL "SELECT l2_ensure_ticket('$BID','merge','$PAY'::jsonb,'$AH',24,1);")
  docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "SELECT l2_approve('$TKT');" >/dev/null 2>&1
  DRUN "import controller; controller.drain_l2_outbox()" >/dev/null
  SETUP_PR="$PR"; SETUP_SHA=$(PSQL "SELECT result_sha FROM approvals WHERE ticket_id='$TKT';"); SETUP_TKT="$TKT"; }
# revert PR(模拟 fixer):还原 BFILE 为 CLEAN;返 PR number
create_revert_pr(){ local CHRUN="$1" BFILE="$2" CLEAN="$3" RVBR PR
  RVBR="fix/$CHRUN-x"
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$RVBR" from_branch="$E2E_BASE_BRANCH" >/dev/null 2>&1
  gw_put_file "$RVBR" "$BFILE" "$CLEAN" "m3c revert"
  PR=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$RVBR" base="$E2E_BASE_BRANCH" title="m3c revert" body=auto 2>&1 | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1)
  inject_complete revert fixer "$CHRUN" "m3c-evt-revert-$TS" >/dev/null
  echo "$PR"; }

# 需求 2:cleanup 用 parent_run_id/revert_run_id(rollback_runs 无 run_id 列)
cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'm3c-%'; DELETE FROM dispatch_outbox WHERE run_id LIKE 'm3c-%' OR room_id LIKE '!m3c-%'; DELETE FROM stage_events WHERE run_id LIKE 'm3c-%' OR event_id LIKE 'm3c-%' OR room_id LIKE '!m3c-%'; DELETE FROM stage_runs WHERE run_id LIKE 'm3c-%'; DELETE FROM rollback_runs WHERE parent_run_id LIKE 'm3c-%' OR revert_run_id LIKE 'm3c-%'; DELETE FROM approvals WHERE run_id LIKE 'm3c-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'm3c-%'; DELETE FROM task_runs WHERE parent_run_id LIKE 'm3c-%'; DELETE FROM task_runs WHERE run_id LIKE 'm3c-%';" >/dev/null 2>&1 || true; }
cleanup_fixture(){ for n in $(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 100 --json number,title -q '.[]|select(.title|test("m3c"))|.number' 2>/dev/null); do gh.exe pr close "$n" --repo "$(e2e_repo)" --delete-branch --comment "M3-C 清理" >/dev/null 2>&1 || true; done
  for b in $(gh.exe api "repos/$(e2e_repo)/branches" --jq '.[].name' 2>/dev/null | grep -E '^fix/m3c-'); do gh.exe api -X DELETE "repos/$(e2e_repo)/git/refs/heads/$b" >/dev/null 2>&1 || true; done; }
restore(){ docker start policy-gw-e2e >/dev/null 2>&1 || true; docker start mergepilot-controller >/dev/null 2>&1 || true; cleanup_db; cleanup_fixture; }
trap '_rc=$?; restore; exit $_rc' EXIT

log "═══════════════════════════════════════════════"
log "  M3-C 状态感知失败处理 + 回滚 验收(fixture=$(e2e_repo))"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker stop mergepilot-controller >/dev/null 2>&1 || true
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "PASS=$PASS FAIL=$FAIL"; exit 1; }
docker cp "$TOOLS/policy-gateway/probe-tools.py" policy-gw-e2e:/tmp/probe-tools.py >/dev/null 2>&1
docker cp "$TOOLS/audit-db/m3c_state.sql" audit-pg:/tmp/m3c_state.sql >/dev/null
docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 -f /tmp/m3c_state.sql >>"$OUT" 2>&1
docker build -t mergepilot-controller:latest "$TOOLS/workflow-controller" >>"$OUT" 2>&1
cleanup_db; cleanup_fixture

# ════════════ 1. 重复 TASK_COMPLETED → 只一次派发(幂等)════════════
log ""; log "=== 1. 重复 TASK_COMPLETED → 只一次下一阶段派发 ==="
RUN1=m3c-dedup-$TS
PSQL "INSERT INTO task_runs(run_id,room_id,repo,pr_number,current_stage,approval_required) VALUES('$RUN1','$ROOM','$(e2e_repo)',1,'review',FALSE) ON CONFLICT DO NOTHING;" >/dev/null
PSQL "INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN1','review','reviewer',1,'RUNNING') ON CONFLICT DO NOTHING;" >/dev/null
inject_complete review reviewer "$RUN1" "m3c-evt-dedup-1-$TS" >/dev/null
inject_complete review reviewer "$RUN1" "m3c-evt-dedup-2-$TS" >/dev/null
FIX_DISP=$(PSQL "SELECT count(*) FROM dispatch_outbox WHERE run_id='$RUN1' AND target_stage='fix';")
FIX_STAGES=$(PSQL "SELECT count(*) FROM stage_runs WHERE run_id='$RUN1' AND stage='fix';")
DUP_EVTS=$(PSQL "SELECT count(*) FROM stage_events WHERE run_id='$RUN1' AND event_type='TASK_COMPLETED' AND status='DUPLICATE';")
logf "  fix dispatch=$FIX_DISP fix stage_runs=$FIX_STAGES dup events=$DUP_EVTS"
[ "$FIX_DISP" = "1" ] && ok "重复 review COMPLETED → 只 1 条 fix dispatch(idempotency_key)" || bad "fix dispatch=$FIX_DISP(应 1)"
[ "$FIX_STAGES" = "1" ] && ok "只 1 条 fix stage_run(uq_stage_attempt)" || bad "fix stage_runs=$FIX_STAGES(应 1)"
[ "$DUP_EVTS" = "1" ] && ok "第 2 个事件标 DUPLICATE(event_id PK 去重)" || bad "dup=$DUP_EVTS(应 1)"

# ════════════ 2. 未合并 FAIL → Fixer 重试(attempt 2,3)════════════
log ""; log "=== 2. 未合并 FAIL → 回退 Fixer(attempt 2/3)==="
RUN2=m3c-retry-$TS
PSQL "INSERT INTO task_runs(run_id,room_id,repo,pr_number,current_stage,approval_required,verify_attempt) VALUES('$RUN2','$ROOM','$(e2e_repo)',2,'verify',FALSE,0) ON CONFLICT DO NOTHING;" >/dev/null
PSQL "INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN2','fix','fixer',1,'COMPLETED') ON CONFLICT DO NOTHING;" >/dev/null
PSQL "INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN2','verify','verifier',1,'RUNNING') ON CONFLICT DO NOTHING;" >/dev/null
inject_complete verify verifier "$RUN2" "m3c-evt-retry-$TS" "VERDICT=FAIL" >/dev/null
VA2=$(PSQL "SELECT verify_attempt FROM task_runs WHERE run_id='$RUN2';")
FIX2=$(PSQL "SELECT attempt FROM stage_runs WHERE run_id='$RUN2' AND stage='fix' ORDER BY attempt DESC LIMIT 1;")
logf "  attempt1 FAIL → verify_attempt=$VA2 fix_attempt=$FIX2"
[ "$VA2" = "1" ] && ok "verify FAIL(attempt 1)→ verify_attempt=1" || bad "verify_attempt=$VA2(应 1)"
[ "$FIX2" = "2" ] && ok "回退 fix attempt=2(stage_runs attempt)" || bad "fix attempt=$FIX2(应 2)"
# 第 2 次 FAIL(verify_attempt 2 → 仍 <MAX=3 继续)
PSQL "UPDATE stage_runs SET status='COMPLETED' WHERE run_id='$RUN2' AND stage='fix' AND attempt=2;" >/dev/null
PSQL "INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN2','verify','verifier',2,'RUNNING') ON CONFLICT DO NOTHING;" >/dev/null
inject_complete verify verifier "$RUN2" "m3c-evt-retry2-$TS" "VERDICT=FAIL" >/dev/null
VA2b=$(PSQL "SELECT verify_attempt FROM task_runs WHERE run_id='$RUN2';")
[ "$VA2b" = "2" ] && ok "第 2 次 FAIL → verify_attempt=2(仍 <MAX=3 继续重试)" || bad "verify_attempt=$VA2b(应 2)"

# ════════════ 3. 未合并 FAIL ×3 → HOLD(MAX),零 revert ════════════
log ""; log "=== 3. 未合并 FAIL ×3(=MAX)→ HOLD,零 revert ==="
RUN3=m3c-max-$TS
PSQL "INSERT INTO task_runs(run_id,room_id,repo,pr_number,current_stage,approval_required,verify_attempt) VALUES('$RUN3','$ROOM','$(e2e_repo)',3,'verify',FALSE,2) ON CONFLICT DO NOTHING;" >/dev/null
PSQL "INSERT INTO stage_runs(run_id,stage,agent,attempt,status) VALUES('$RUN3','verify','verifier',3,'RUNNING') ON CONFLICT DO NOTHING;" >/dev/null
inject_complete verify verifier "$RUN3" "m3c-evt-max-$TS" "VERDICT=FAIL" >/dev/null
ST3=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN3';")
RB3=$(PSQL "SELECT count(*) FROM rollback_runs WHERE parent_run_id='$RUN3';")
logf "  task=$ST3 rollback_runs=$RB3"
[ "$ST3" = "HOLD" ] && ok "第 3 次 FAIL(=MAX)→ HOLD(不自动 CLOSE)" || bad "task=$ST3(应 HOLD)"
[ "$RB3" = "0" ] && ok "未合并 FAIL 零 revert(未触发回滚)" || bad "rollback_runs=$RB3(应 0)"

# ════════════ 4. POST_MERGE_VERIFY_FAILED 伪造 result_sha → 拒(真实入口,需求 2/3)════════════
log ""; log "=== 4. POST_MERGE_VERIFY_FAILED 伪造 result_sha → 拒(经 process_event 真实入口) ==="
RUN4=m3c-forged-$TS
PSQL "INSERT INTO task_runs(run_id,room_id,repo,pr_number,current_stage,approval_required,status) VALUES('$RUN4','$ROOM','$(e2e_repo)',4,'l2_done',FALSE,'MERGED') ON CONFLICT DO NOTHING;" >/dev/null
PSQL "INSERT INTO approvals(ticket_id,run_id,action,repo,pr_number,status,result_sha,canonical_payload,args_hash) VALUES('tkt-m3c-forged-$TS','$RUN4','merge','$(e2e_repo)',4,'USED','realsha12345678901234567890123456789012345678','{}'::jsonb,'x') ON CONFLICT DO NOTHING;" >/dev/null
inject_pmf "$RUN4" "$(e2e_repo)" 4 "FORGED-DIFFERENT-SHA-NOT-40HEX" "m3c-evt-forged-$TS" >/dev/null
RB4=$(PSQL "SELECT count(*) FROM rollback_runs WHERE parent_run_id='$RUN4';")
EV4=$(PSQL "SELECT status FROM stage_events WHERE event_id='m3c-evt-forged-$TS';")
logf "  rollback_runs=$RB4 event_status=$EV4"
[ "$RB4" = "0" ] && ok "伪造 result_sha → 不建 rollback_runs" || bad "rollback_runs=$RB4(应 0)"
[ "$EV4" = "ERROR" ] && ok "事件标 ERROR(result_sha 校验拒)" || bad "event=$EV4(应 ERROR)"

# ════════════ 5. 合法 POST_MERGE_VERIFY_FAILED → rollback PENDING + child run ════════════
log ""; log "=== 5. 合法 POST_MERGE_VERIFY_FAILED → rollback PENDING + child run ==="
RUN5=m3c-legit-$TS; REAL_SHA5=$(python3 -c "import hashlib;print(hashlib.sha256(b'$TS-$RANDOM').hexdigest()[:40])")
PSQL "INSERT INTO task_runs(run_id,room_id,repo,pr_number,current_stage,approval_required,status) VALUES('$RUN5','$ROOM','$(e2e_repo)',5,'l2_done',FALSE,'MERGED') ON CONFLICT DO NOTHING;" >/dev/null
PSQL "INSERT INTO approvals(ticket_id,run_id,action,repo,pr_number,status,result_sha,canonical_payload,args_hash) VALUES('tkt-m3c-pmf-$TS','$RUN5','merge','$(e2e_repo)',5,'USED','$REAL_SHA5','{}'::jsonb,'x') ON CONFLICT DO NOTHING;" >/dev/null
inject_pmf "$RUN5" "$(e2e_repo)" 5 "$REAL_SHA5" "m3c-evt-pmf-$TS" >/dev/null
RB5=$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN5';")
CHILD5=$(PSQL "SELECT count(*) FROM task_runs WHERE run_id='${RUN5}-revert-${REAL_SHA5:0:8}' AND parent_run_id='$RUN5';")
logf "  rollback_runs.status=$RB5 child_run=${RUN5}-revert-${REAL_SHA5:0:8} exists=$CHILD5"
[ "$RB5" = "PENDING" ] && ok "合法 POST_MERGE_VERIFY_FAILED → rollback_runs PENDING" || bad "rb=$RB5(应 PENDING)"
[ "$CHILD5" = "1" ] && ok "建 child revert run(parent_run_id 回链)" || bad "child run=$CHILD5(应 1)"

# ════════════ 6/7/8. 已合并 FAIL → revert child run → approve → merge → ROLLED_BACK → reverify PASS → RECOVERED ════════════
log ""; log "=== 6/7/8. 已合并 FAIL → revert child run(container 真 loop)→ REVERTED → reverify PASS → RECOVERED ==="
RUN6=m3c-rb-$TS; BFILE6="m3c-$TS-app.txt"; CLEAN6="CLEAN_BASELINE_$TS"; BAD6="BAD_CONTENT_$TS"
setup_bad_merge "$RUN6" "$BFILE6" "$CLEAN6" "$BAD6"
BAD_SHA="$SETUP_SHA"; PR6="$SETUP_PR"
logf "  bad fix PR=#$PR6 bad_merge_sha=${BAD_SHA:0:12}"
if [ -z "$BAD_SHA" ] || [ "$BAD_SHA" = "None" ]; then
  bad "回滚全链: bad merge 未完成(无 result_sha)"
else
  T6m=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN6';")
  [ "$T6m" = "MERGED" ] && ok "bad fix PR squash-merge → task MERGED(result_sha 固化)" || bad "task=$T6m(应 MERGED)"
  # 真实入口注入合法 PMF(文件由 controller 经 get_commit 权威派生,不信 event)
  inject_pmf "$RUN6" "$(e2e_repo)" "$PR6" "$BAD_SHA" "m3c-evt-rb-$TS" >/dev/null
  CHILD6=$(child_run_for "$RUN6" "$BAD_SHA")
  # container 跑真 loop(PENDING → REVERT_PR_OPEN)
  start_ctrl m3c-ctrl6
  for i in $(seq 1 20); do [ "$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN6';")" = "REVERT_PR_OPEN" ] && break; sleep 2; done
  RBST6a=$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN6';")
  logf "  回滚阶段: rollback=$RBST6a child=$CHILD6"
  [ "$RBST6a" = "REVERT_PR_OPEN" ] && ok "PENDING → 冲突检测通过 → 派 fixer 建 revert PR(REVERT_PR_OPEN)" || { bad "未到 REVERT_PR_OPEN: $RBST6a"; save_logs m3c-ctrl6; }
  if [ "$RBST6a" = "REVERT_PR_OPEN" ]; then
    RVPR6=$(create_revert_pr "$CHILD6" "$BFILE6" "$CLEAN6")
    logf "  revert PR=#$RVPR6 branch=fix/$CHILD6-x"
    REVT6=$(PSQL "SELECT status FROM stage_runs WHERE run_id='$CHILD6' AND stage='revert' ORDER BY attempt DESC LIMIT 1;")
    [ "$REVT6" = "COMPLETED" ] && ok "child revert TASK_COMPLETED → stage COMPLETED" || bad "revert stage=$REVT6(应 COMPLETED)"
    for i in $(seq 1 20); do [ "$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN6';")" = "AWAITING_APPROVAL" ] && break; sleep 2; done
    RBST6b=$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN6';")
    RVTKT6=$(PSQL "SELECT revert_ticket_id FROM rollback_runs WHERE parent_run_id='$RUN6';")
    [ "$RBST6b" = "AWAITING_APPROVAL" ] && ok "revert PR 发现 + L2 票(merge_method=merge)→ AWAITING_APPROVAL" || bad "未到 AWAITING_APPROVAL: $RBST6b"
    [ -n "$RVTKT6" ] && bash "$TOOLS/approve.sh" approve "$RVTKT6" >>"$OUT" 2>&1 || true
    for i in $(seq 1 25); do [ "$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN6';")" = "ROLLED_BACK" ] && break; sleep 2; done
    T6r=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN6';")
    RVSHA6=$(PSQL "SELECT revert_result_sha FROM rollback_runs WHERE parent_run_id='$RUN6';")
    logf "  revert merge 后: parent task=$T6r revert_sha=${RVSHA6:0:12}"
    [ "$T6r" = "ROLLED_BACK" ] && ok "revert PR 真合并 → parent ROLLED_BACK(决策 8:仅真实 merge 后)" || bad "task=$T6r(应 ROLLED_BACK)"
    [ -n "$RVSHA6" ] && ok "revert_result_sha 固化(child run MERGED)" || bad "revert_result_sha 空"
    # reverify PASS → RECOVERED(reverify 派发在 parent run)
    inject_complete reverify verifier "$RUN6" "m3c-evt-rvpass-$TS" "VERDICT=PASS" >/dev/null
    for i in $(seq 1 10); do [ "$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN6';")" = "RECOVERED" ] && break; sleep 2; done
    RBST6c=$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN6';"); CS6c=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN6';")
    [ "$RBST6c" = "RECOVERED" ] && ok "reverify PASS → rollback RECOVERED" || bad "rollback=$RBST6c(应 RECOVERED)"
    [ "$CS6c" = "reverified" ] && ok "parent current_stage=reverified" || bad "stage=$CS6c(应 reverified)"
    # GitHub 侧:BFILE 已还原为 CLEAN(需求 4:经修复后的 probe 读真实内容)
    CUR6=$(e2e_GW coordinator --call get_file_contents owner="$E2E_OWNER" repo="$E2E_REPO" path="$BFILE6" ref=main 2>&1 | head -1)
    has "$CUR6" "CLEAN_BASELINE" && ok "fixture main 上 BFILE 已还原为 CLEAN(真 revert 生效)" || bad "BFILE 未还原: $CUR6"
  fi
  save_logs m3c-ctrl6
fi

# ════════════ 9. reverify FAIL → HOLD,不二回滚(幂等:重复 PMF 不新增)════════════
log ""; log "=== 9. reverify FAIL → HOLD,不二回滚 + 重复 PMF 幂等 ==="
RUN9=m3c-rvfail-$TS; BFILE9="m3c-rv-$TS.txt"; CLEAN9="CLEAN9_$TS"; BAD9="BAD9_$TS"
setup_bad_merge "$RUN9" "$BFILE9" "$CLEAN9" "$BAD9"
BAD9_SHA="$SETUP_SHA"; PR9="$SETUP_PR"
if [ -z "$BAD9_SHA" ] || [ "$BAD9_SHA" = "None" ]; then
  bad "rvfail: bad merge 未完成"
else
  inject_pmf "$RUN9" "$(e2e_repo)" "$PR9" "$BAD9_SHA" "m3c-evt-rvfail-$TS" >/dev/null
  CHILD9=$(child_run_for "$RUN9" "$BAD9_SHA")
  start_ctrl m3c-ctrl9
  for i in $(seq 1 20); do [ "$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN9';")" = "REVERT_PR_OPEN" ] && break; sleep 2; done
  create_revert_pr "$CHILD9" "$BFILE9" "$CLEAN9" >/dev/null
  for i in $(seq 1 20); do [ "$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN9';")" = "AWAITING_APPROVAL" ] && break; sleep 2; done
  RVTKT9=$(PSQL "SELECT revert_ticket_id FROM rollback_runs WHERE parent_run_id='$RUN9';")
  [ -n "$RVTKT9" ] && bash "$TOOLS/approve.sh" approve "$RVTKT9" >>"$OUT" 2>&1 || true
  for i in $(seq 1 25); do [ "$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN9';")" = "ROLLED_BACK" ] && break; sleep 2; done
  inject_complete reverify verifier "$RUN9" "m3c-evt-rvf-$TS" "VERDICT=FAIL" >/dev/null
  for i in $(seq 1 10); do RB9x=$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN9';"); [ "$RB9x" = "HELD" ] && break; sleep 2; done
  T9=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN9';"); RB9=$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN9';")
  logf "  reverify FAIL → parent=$T9 rollback=$RB9"
  [ "$T9" = "HOLD" ] && ok "reverify FAIL → parent HOLD(人工升级)" || bad "parent=$T9(应 HOLD)"
  [ "$RB9" = "HELD" ] && ok "rollback → HELD" || bad "rollback=$RB9(应 HELD)"
  # 幂等/不二回滚:重复注入同一 PMF → 不新增 rollback_runs
  inject_pmf "$RUN9" "$(e2e_repo)" "$PR9" "$BAD9_SHA" "m3c-evt-rvfail2-$TS" >/dev/null
  RB9_CNT=$(PSQL "SELECT count(*) FROM rollback_runs WHERE parent_run_id='$RUN9';")
  logf "  重复 PMF 后 rollback_count=$RB9_CNT"
  [ "$RB9_CNT" = "1" ] && ok "重复 PMF → UNIQUE(parent_run,bad_sha)幂等,不二回滚(count=1)" || bad "rollback_count=$RB9_CNT(应 1)"
  save_logs m3c-ctrl9
fi

# ════════════ 10. 未审批 revert:drain 跳过 PENDING + 显式 Gateway → CLAIM_MISMATCH ════════════
log ""; log "=== 10. 未审批 revert:10a.drain 跳过 PENDING  10b.显式 Gateway → CLAIM_MISMATCH ==="
RUN10=m3c-unapv-$TS; BFILE10="m3c-ua-$TS.txt"; CLEAN10="CLEAN10_$TS"; BAD10="BAD10_$TS"
setup_bad_merge "$RUN10" "$BFILE10" "$CLEAN10" "$BAD10"
BAD10_SHA="$SETUP_SHA"; PR10="$SETUP_PR"
if [ -z "$BAD10_SHA" ] || [ "$BAD10_SHA" = "None" ]; then
  bad "unapv: bad merge 未完成"
else
  inject_pmf "$RUN10" "$(e2e_repo)" "$PR10" "$BAD10_SHA" "m3c-evt-ua-$TS" >/dev/null
  CHILD10=$(child_run_for "$RUN10" "$BAD10_SHA")
  start_ctrl m3c-ctrl10
  for i in $(seq 1 20); do [ "$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN10';")" = "REVERT_PR_OPEN" ] && break; sleep 2; done
  create_revert_pr "$CHILD10" "$BFILE10" "$CLEAN10" >/dev/null
  for i in $(seq 1 20); do [ "$(PSQL "SELECT status FROM rollback_runs WHERE parent_run_id='$RUN10';")" = "AWAITING_APPROVAL" ] && break; sleep 2; done
  RVTKT10=$(PSQL "SELECT revert_ticket_id FROM rollback_runs WHERE parent_run_id='$RUN10';")
  save_logs m3c-ctrl10
  # 10a. 不 approve → drain 跳过 PENDING(票不 APPROVED,不 merge;child 不 MERGED)
  DRUN "import controller; controller.drain_l2_outbox()" >/dev/null
  RVAPV10=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$RVTKT10';")
  CHM10=$(PSQL "SELECT status FROM task_runs WHERE run_id='$CHILD10';")
  logf "  10a 未审批 drain: revert 票=$RVAPV10 child=$CHM10"
  [ "$RVAPV10" = "PENDING" ] && ok "10a: drain 跳过 PENDING 票(未 APPROVED 不 merge)" || bad "票=$RVAPV10(应 PENDING)"
  [ "$CHM10" != "MERGED" ] && ok "10a: child run 未被 merge(无审批不执行)" || bad "child=$CHM10(应非 MERGED)"
  # 10b. 显式 Gateway 调 merge(PENDING 票)→ CLAIM_MISMATCH(l2_claim_ticket MISMATCH)
  #   DRUN 内部重定向到 run-raw.log,故从 run-raw.log grep 结果(不接 DRUN 管道)
  DRUN "import controller,gateway_client
try:
    gateway_client.gateway_call('merge_pull_request', {'owner':'$E2E_OWNER','repo':'$E2E_REPO','pullNumber':$PR10,'approval_ticket':'$RVTKT10','merge_method':'merge'}, timeout=15)
    print('CLAIM=ALLOWED')
except gateway_client.GatewayDenied as e:
    print('CLAIM=' + str(e.reason_code))
except Exception as e:
    print('CLAIM=ERR:' + type(e).__name__)
"
  CLAIM=$(grep -oE 'CLAIM=[A-Z_]+' "$EV/run-raw.log" | tail -1)
  CM_AUDIT=$(PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='$RVTKT10' AND reason_code='CLAIM_MISMATCH';")
  logf "  10b 显式 merge: $CLAIM  audit CLAIM_MISMATCH=$CM_AUDIT"
  has "$CLAIM" "CLAIM_MISMATCH" && ok "10b: 显式 Gateway merge(PENDING 票)→ CLAIM_MISMATCH" || bad "claim=$CLAIM(应 CLAIM_MISMATCH)"
fi

# ════════════ 证据 + 审计 ════════════
log ""; log "=== 证据 + 审计 ==="
PSQL "SELECT t.run_id,t.status,t.current_stage,t.verify_attempt,rb.status AS rb,rb.reverted_merge_sha,rb.revert_result_sha,rb.reverify_verdict FROM task_runs t LEFT JOIN rollback_runs rb ON rb.parent_run_id=t.run_id WHERE t.run_id LIKE 'm3c-%' ORDER BY t.run_id;" > "$EV/db-snapshot.txt" 2>/dev/null
PSQL "SELECT parent_run_id,revert_run_id,status,revert_branch,revert_pr_number,revert_ticket_id,revert_result_sha,fail_reason,reverify_verdict FROM rollback_runs WHERE parent_run_id LIKE 'm3c-%' ORDER BY parent_run_id;" > "$EV/rollback-runs.txt" 2>/dev/null
cp "$OUT" "$EV/m3c-transcript.txt" 2>/dev/null || true
# 需求 6:容器日志已落盘(非空)
if [ -s "$EV/controller-logs.txt" ]; then ok "容器日志已正确保存(非空)"; else bad "容器日志为空(保存失败)"; fi
# 凭据/AI 标识扫描
set +e; grep -rnoE 'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80}|sk-[A-Za-z0-9]{20,}' "$EV" "$TOOLS/m3c-e2e.sh" >/dev/null 2>&1 && bad "凭证格式泄漏?" || ok "无凭证格式泄漏"
grep -rniE 'co-authored|claude|anthropic|🤖' "$TOOLS/m3c-e2e.sh" "$EV" 2>/dev/null | grep -v "grep -rniE" | grep -q . && bad "AI 标识?" || ok "无 AI 标识"

# ════════════ 收尾 + 硬门 ════════════
log ""; log "=== 收尾 + 硬门 ==="
cleanup_db; cleanup_fixture
DB_LEFT=$(PSQL "SELECT (SELECT count(*) FROM dispatch_outbox WHERE run_id LIKE 'm3c-%' OR room_id LIKE '!m3c-%') + (SELECT count(*) FROM stage_events WHERE run_id LIKE 'm3c-%' OR event_id LIKE 'm3c-%' OR room_id LIKE '!m3c-%') + (SELECT count(*) FROM stage_runs WHERE run_id LIKE 'm3c-%') + (SELECT count(*) FROM rollback_runs WHERE parent_run_id LIKE 'm3c-%' OR revert_run_id LIKE 'm3c-%') + (SELECT count(*) FROM approvals WHERE run_id LIKE 'm3c-%') + (SELECT count(*) FROM run_pr_bindings WHERE run_id LIKE 'm3c-%') + (SELECT count(*) FROM task_runs WHERE run_id LIKE 'm3c-%' OR parent_run_id LIKE 'm3c-%');")
[ "$DB_LEFT" = "0" ] && ok "M3-C DB 残留=0(不污染后续回归)" || bad "M3-C DB 残留=$DB_LEFT"
# GitHub API eventual-consistency:重试清理 + 等待,直到 0 PR/仅 main(合并分支删除后 LIST 可能短暂滞后)
for i in $(seq 1 6); do
  OPEN_PRS=$(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 100 --json number -q '.|length' 2>/dev/null || echo "?")
  BRANCHES=$(gh.exe api "repos/$(e2e_repo)/branches" --jq '[.[].name]|join(",")' 2>/dev/null || echo "?")
  { [ "$OPEN_PRS" = "0" ] && [ "$BRANCHES" = "main" ]; } && break
  cleanup_fixture; sleep 3
done
logf "  fixture 终态: openPRs=$OPEN_PRS branches=$BRANCHES"
[ "$OPEN_PRS" = "0" ] && ok "fixture 0 open PR" || bad "open PR=$OPEN_PRS"
[ "$BRANCHES" = "main" ] && ok "fixture 仅 main" || bad "branches=$BRANCHES"
sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$OUT" 2>/dev/null || true

log ""
log "═══════════════════════════════════════════════"
log "  M3-C 验收: PASS=$PASS / EXPECTED=$EXPECTED_PASS  FAIL=$FAIL"
log "═══════════════════════════════════════════════"
cp "$OUT" "$EV/m3c-transcript.txt" 2>/dev/null || true
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED_PASS" ]; then exit 0; else exit 1; fi
