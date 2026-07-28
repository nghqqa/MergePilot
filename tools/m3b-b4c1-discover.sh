#!/bin/bash
# m3b-b4c1-discover.sh — B4c-1.2 绑定发现验收(权威身份 + branch 双源 + 原子 CAS + RETRY 不累计)。
# 停 controller loop,用 docker run --rm 一次性容器调 discover(避免 loop 竞争 + CAS 需 current_stage=l2_binding);
# NOT_FOUND 走 loop(重启 controller)。discover 经 GATEWAY_URL 可注入坏值测 RETRY。
# 覆盖:FOUND(权威 head_sha==PR head==branch sha)/ 幂等 / UPDATED / AMBIGUOUS→HOLD /
#   NOT_FOUND→attempts→HOLD(loop) / RETRY(Gateway 不可达)不累计 attempts。
set -uo pipefail
# ── Step 2 安全门 ──
# 本脚本在 B4c 闭合时固化于生产仓 nghqqa/MergePilot(frozen 证据脚本)。重跑会写生产仓
# → 默认拒。重跑需 export ALLOW_PRODUCTION_E2E=1(留痕);或迁 fixture(见 tools/e2e-lib.sh
# + evidence/m3b-b4c/step2-fixture/)。新 E2E(B4d+)默认走 fixture,不经此门。
source "$(dirname "$0")/e2e-lib.sh"
[ "${ALLOW_PRODUCTION_E2E:-0}" = "1" ] || { echo "REFUSED: $0 固化于生产仓 nghqqa/MergePilot;重跑需 ALLOW_PRODUCTION_E2E=1 或迁 fixture(见 e2e-lib.sh)" >&2; exit 2; }
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c/1-discover
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out 2>/dev/null || true
OUT="$EV/discover-test.out"; : > "$OUT"
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
cleanup_runs(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4c1-%'; DELETE FROM approvals WHERE run_id LIKE 'b4c1-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4c1-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4c1-%';" >/dev/null 2>&1 || true; }
restore(){ docker start "$NAME" >/dev/null 2>&1 || true; cleanup_runs; }
trap restore EXIT

log "═══════════════════════════════════════════════"
log "  B4c-1.2 绑定发现验收(权威身份 + branch 双源 + CAS + RETRY)"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
docker cp /mnt/d/goai/mergepilot-os/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py >/dev/null 2>&1
cleanup_runs
# 停 controller loop,避免与直接 discover 竞争(section 6 重启)
docker stop "$NAME" >/dev/null 2>&1 || true
log "  controller loop stopped(direct discover via one-shot)"

# ─── 0. 构建镜像 + 容器内源码哈希 == 仓库 + schema 单元测试(确定性负向)───
log ""; log "=== 0. build image + container source hash + schema unit ==="
docker build -t "$IMG" /mnt/d/goai/mergepilot-os/tools/workflow-controller/ >>"$OUT" 2>&1 || { bad "镜像 build 失败"; exit 1; }
for f in controller.py gateway_client.py test_gateway_schema.py; do
  ch=$(docker run --rm "$IMG" python3 -c "import hashlib;print(hashlib.sha256(open('/app/$f','rb').read()).hexdigest()[:16])" 2>/dev/null)
  rh=$(sha256sum "/mnt/d/goai/mergepilot-os/tools/workflow-controller/$f" | cut -c1-16)
  [ "$ch" = "$rh" ] && ok "$f 容器内 == 仓库(镜像对应当前 commit)" || bad "$f 漂移(container=$ch repo=$rh)"
done
docker run --rm "$IMG" python3 /app/test_gateway_schema.py > "$EV/schema-unit.out" 2>&1
tail -1 "$EV/schema-unit.out" | tee -a "$OUT"
grep -q "FAIL=0" "$EV/schema-unit.out" && ok "schema 单元 PASS(40hex/number-bool/head_repo 缺失/short-sha 负向)" || { bad "schema 单元 FAIL"; tail -8 "$EV/schema-unit.out" | tee -a "$OUT"; }

# discover 经一次性容器:$1=run_id, $2=GATEWAY_URL(默认真实;注入坏值测 RETRY)
discover(){ local RUN="$1" GWU="${2:-http://policy-gw:8083}"
  docker run --rm --network hiclab-net --env-file "$ENVF" \
    -e PG_HOST=audit-pg -e PG_DATABASE=mergepilot_audit -e PG_USER=mergepilot \
    -e GATEWAY_URL="$GWU" -e COORDINATOR_TOKEN="$COORD" -e L2_MERGE_ENABLED=0 \
    -e L2_GW_TIMEOUT=60 \
    "$IMG" python3 -c "
import controller, json
s,i = controller.discover_binding_for_run('$RUN')
print('STATUS='+str(s)); print('INFO='+json.dumps(i, default=str))
" 2>&1 | grep -E "^STATUS=|^INFO="
}
create_fix_pr(){ local BR="$1" P="$2" L="$3" R
  GW fixer --call create_branch owner=nghqqa repo=MergePilot branch="$BR" from_branch=main 2>&1 | grep -qi ref && logf "  分支 $BR 建好" || logf "  分支 $BR 可能已存在"
  GW fixer --call create_or_update_file owner=nghqqa repo=MergePilot path="$P" branch="$BR" content="b4c1-$L-$TS" message="b4c1 $L" 2>&1 | grep -qi "commit\|sha\|content" && logf "  commit 加好($P)"
  R=$(GW fixer --call create_pull_request owner=nghqqa repo=MergePilot head="$BR" base=main title="B4c-1 $L" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1
}
pr_head_sha(){ GW reviewer --call pull_request_read method=get owner=nghqqa repo=MergePilot pullNumber="$1" 2>&1 | grep -oE '[0-9a-f]{40}' | head -1; }
branch_sha(){ GW reviewer --call list_branches owner=nghqqa repo=MergePilot 2>&1 | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(next((b['sha'] for b in d if b.get('name')=='$1'),''))" 2>/dev/null; }
mkrun(){ PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required,l2_discovery_attempts) VALUES('$1','APPROVAL_PENDING','nghqqa/MergePilot',0,'l2_binding',TRUE,${2:-0}) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_binding',l2_discovery_attempts=${2:-0},approval_required=TRUE;" >/dev/null; }
reset_binding_stage(){ PSQL "UPDATE task_runs SET status='APPROVAL_PENDING', current_stage='l2_binding' WHERE run_id='$1';" >/dev/null; }

# ─── 1. FOUND:权威 head_sha == PR head == branch sha ───
log ""; log "=== 1. FOUND: 1 PR → 绑定(head_sha==PR head==branch sha) ==="
RUN1=b4c1-found-$TS; BR1=fix/$RUN1-extra
mkrun "$RUN1"
PR1=$(create_fix_pr "$BR1" "disc-found-$TS.md" "found")
if [ -z "$PR1" ]; then bad "FOUND: PR 创建失败"; else
  PRSHA=$(pr_head_sha "$PR1"); BSHA=$(branch_sha "$BR1")
  log "  PR#$PR1 head=${PRSHA:0:12} branch=${BSHA:0:12}"
  [ -n "$PRSHA" ] && [ "$PRSHA" = "$BSHA" ] && ok "PR head.sha == branch ref sha(双源一致,可固化)" || bad "双源不一致或空: pr=$PRSHA branch=$BSHA"
  D1=$(discover "$RUN1")
  logf "  discover: $(echo "$D1" | grep '^STATUS=' | head -1)"
  echo "$D1" | grep -q "^STATUS=FOUND" && ok "FOUND: discover 返回 FOUND" || bad "应 FOUND: $(echo "$D1"|head -1)"
  BIND_SHA=$(PSQL "SELECT head_sha FROM run_pr_bindings WHERE run_id='$RUN1';")
  BIND_PR=$(PSQL "SELECT pr_number FROM run_pr_bindings WHERE run_id='$RUN1';")
  [ "$BIND_SHA" = "$PRSHA" ] && ok "binding head_sha == GitHub 权威(双源校验过)" || bad "head_sha 不匹配: $BIND_SHA vs $PRSHA"
  [ "$BIND_PR" = "$PR1" ] && ok "binding pr_number==$PR1" || bad "pr_number 异常: $BIND_PR"
  STAGE=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN1';")
  [ "$STAGE" = "l2_awaiting_ticket" ] && ok "FOUND → 推进 current_stage=l2_awaiting_ticket(原子 CAS)" || bad "stage 未推进: $STAGE"
fi

# ─── 2. 幂等:reset stage 再 discover → 同 binding,无重复 ───
log ""; log "=== 2. 幂等:重复 discover → 同 binding(无重复行) ==="
BID_BEFORE=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN1';")
CNT_BEFORE=$(PSQL "SELECT count(*) FROM run_pr_bindings WHERE run_id='$RUN1';")
reset_binding_stage "$RUN1"
D2=$(discover "$RUN1")
BID_AFTER=$(PSQL "SELECT binding_id FROM run_pr_bindings WHERE run_id='$RUN1';")
CNT_AFTER=$(PSQL "SELECT count(*) FROM run_pr_bindings WHERE run_id='$RUN1';")
echo "$D2" | grep -q "^STATUS=FOUND" && ok "重复 discover 返回 FOUND" || bad "重复应 FOUND: $(echo "$D2"|head -1)"
[ "$BID_BEFORE" = "$BID_AFTER" ] && ok "同 binding_id(幂等)" || bad "binding_id 变: $BID_BEFORE→$BID_AFTER"
[ "$CNT_AFTER" = "$CNT_BEFORE" ] && ok "无新行(仍 $CNT_AFTER 条)" || bad "多建: $CNT_AFTER"

# ─── 3. UPDATED:corrupt stored head → discover 刷新 ───
log ""; log "=== 3. UPDATED: stored head 不一致 → 刷新回 GitHub 真值 ==="
REAL_SHA=$(pr_head_sha "$PR1")
PSQL "UPDATE run_pr_bindings SET head_sha='0deadbeef00000000000000000000000000000force' WHERE run_id='$RUN1';" >/dev/null
CORRUPT=$(PSQL "SELECT head_sha FROM run_pr_bindings WHERE run_id='$RUN1';")
reset_binding_stage "$RUN1"
D3=$(discover "$RUN1")
BIND3=$(PSQL "SELECT head_sha FROM run_pr_bindings WHERE run_id='$RUN1';")
echo "$D3" | grep -q "^STATUS=UPDATED" && ok "stored≠GitHub → UPDATED" || bad "应 UPDATED: $(echo "$D3"|head -1)"
[ "$BIND3" = "$REAL_SHA" ] && ok "刷新回 GitHub 真值(不静默保留假值)" || bad "未刷新: $BIND3 vs $REAL_SHA"
[ "$BIND3" != "$CORRUPT" ] && ok "假值已覆盖" || bad "假值仍存"

# ─── 4. AMBIGUOUS:2 fix PR → HOLD ───
log ""; log "=== 4. AMBIGUOUS: 2 fix PR → HOLD ==="
RUN4=b4c1-ambig-$TS
mkrun "$RUN4"
create_fix_pr "fix/$RUN4-one" "disc-ambig-a-$TS.md" "ambig-a" >/dev/null
create_fix_pr "fix/$RUN4-two" "disc-ambig-b-$TS.md" "ambig-b" >/dev/null
D4=$(discover "$RUN4")
echo "$D4" | grep -q "^STATUS=AMBIGUOUS" && ok "2 PR → AMBIGUOUS" || bad "应 AMBIGUOUS: $(echo "$D4"|head -1)"
ST4=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN4';")
[ "$ST4" = "HOLD" ] && ok "AMBIGUOUS → task HOLD" || bad "AMBIGUOUS 应 HOLD: $ST4"
B4=$(PSQL "SELECT count(*) FROM run_pr_bindings WHERE run_id='$RUN4';")
[ "$B4" = "0" ] && ok "AMBIGUOUS 未写 binding" || bad "却写了 binding"

# ─── 5. RETRY(Gateway 不可达)不累计 attempts ───
log ""; log "=== 5. RETRY: Gateway 不可达 → 不累计 l2_discovery_attempts ==="
RUN5=b4c1-retry-$TS
mkrun "$RUN5" 0
ATT_BEFORE=$(PSQL "SELECT l2_discovery_attempts FROM task_runs WHERE run_id='$RUN5';")
D5=$(discover "$RUN5" "http://policy-gw-unreachable:9999")
ATT_AFTER=$(PSQL "SELECT l2_discovery_attempts FROM task_runs WHERE run_id='$RUN5';")
ST5=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN5';")
logf "  discover(bad gw): $(echo "$D5" | grep '^STATUS=' | head -1)"
echo "$D5" | grep -q "^STATUS=RETRY" && ok "Gateway 不可达 → RETRY" || bad "应 RETRY: $(echo "$D5"|head -1)"
[ "$ATT_AFTER" = "$ATT_BEFORE" ] && ok "RETRY 不累计 attempts($ATT_BEFORE→$ATT_AFTER)" || bad "RETRY 不应累计: $ATT_BEFORE→$ATT_AFTER"
[ "$ST5" = "APPROVAL_PENDING" ] && ok "RETRY 后 task 仍 APPROVAL_PENDING(未误 HOLD)" || bad "RETRY 后状态异常: $ST5"

# ─── 6. NOT_FOUND:直接验状态 + loop 驱动 attempts→HOLD ───
log ""; log "=== 6. NOT_FOUND: 0 PR 累计 attempts → loop 达阈值 HOLD ==="
RUN6=b4c1-notfound-$TS   # 不建任何 fix/b4c1-notfound-* PR
mkrun "$RUN6" 0
docker stop "$NAME" >/dev/null 2>&1   # 停 loop,直接验 NOT_FOUND
D6=$(discover "$RUN6")
ATT6=$(PSQL "SELECT l2_discovery_attempts FROM task_runs WHERE run_id='$RUN6';")
echo "$D6" | grep -q "^STATUS=NOT_FOUND" && ok "0 PR → NOT_FOUND(查询成功且确实为零)" || bad "应 NOT_FOUND: $(echo "$D6"|head -1)"
[ "$ATT6" = "1" ] && ok "NOT_FOUND 累计 attempts 0→1" || bad "NOT_FOUND 应累计到 1: 实际 $ATT6"
# 预置 max-1(controller 默认 L2_DISCOVERY_MAX=3,故 2),启 loop,一轮 NOT_FOUND 即达阈值→HOLD
PSQL "UPDATE task_runs SET current_stage='l2_binding', l2_discovery_attempts=2 WHERE run_id='$RUN6';" >/dev/null 2>&1
docker start "$NAME" >/dev/null 2>&1
log "  等 loop 处理(最多 ~150s)..."
HELD=0
for i in $(seq 1 18); do
  S6=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN6';")
  [ "$S6" = "HOLD" ] && { HELD=1; break; }
  sleep 9
done
CS6=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN6';")
[ "$HELD" = "1" ] && ok "NOT_FOUND loop → HOLD(stage=$CS6)" || bad "未达 HOLD(status=$S6 stage=$CS6)"

# ─── 8. binding 身份冲突 → 同事务置 HOLD(P1-1:DB 真正 HOLD,不每 tick 重复)───
log ""; log "=== 8. binding conflict → DB HOLD(同事务,不重复) ==="
RUN8=b4c1-conflict-$TS; BR8=fix/$RUN8-extra
mkrun "$RUN8"
PR8=$(create_fix_pr "$BR8" "disc-conflict-$TS.md" "conflict")
if [ -z "$PR8" ]; then bad "conflict: PR 创建失败"; else
  # 预置 binding:用**真实 PR8 number** 但**错误 fix_branch**(身份冲突——stored ≠ GitHub 当前)
  FAKEBID="bnd-fake-conflict-$TS"
  FAKE_SHA=$(python3 -c 'print("a"*40)')
  PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$FAKEBID','$RUN8','nghqqa/MergePilot',$PR8,'fix/$RUN8-WRONGBRANCH','main','$FAKE_SHA') ON CONFLICT (binding_id) DO UPDATE SET run_id=EXCLUDED.run_id,pr_number=EXCLUDED.pr_number,fix_branch=EXCLUDED.fix_branch,head_sha=EXCLUDED.head_sha;" >/dev/null
  PRE=$(PSQL "SELECT fix_branch FROM run_pr_bindings WHERE run_id='$RUN8';")
  [ "$PRE" = "fix/$RUN8-WRONGBRANCH" ] && logf "  预置 binding(pr=$PR8 但 fix_branch=WRONG)已就绪" || { bad "预置 binding 插入失败(pre=$PRE)"; }
  reset_binding_stage "$RUN8"
  D8=$(discover "$RUN8")
  ST8=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN8';")
  CS8=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN8';")
  LE8=$(PSQL "SELECT last_error FROM task_runs WHERE run_id='$RUN8';")
  logf "  discover: $(echo "$D8" | grep '^STATUS=' | head -1)"
  logf "  DB: status=$ST8 stage=$CS8 err=${LE8:0:50}"
  echo "$D8" | grep -q "^STATUS=HOLD_BINDING_CONFLICT" && ok "conflict → HOLD_BINDING_CONFLICT" || bad "应 conflict: $(echo "$D8"|head -1)"
  [ "$ST8" = "HOLD" ] && ok "DB status=HOLD(同事务迁移)" || bad "DB 未 HOLD: $ST8"
  [ "$CS8" = "l2_binding_failed" ] && ok "current_stage=l2_binding_failed" || bad "stage 异常: $CS8"
  echo "$LE8" | grep -q "HOLD_BINDING_CONFLICT" && ok "last_error 含 HOLD_BINDING_CONFLICT" || bad "last_error 缺冲突标记"
fi

# ─── 9. CAS CONCURRENT:阶段已改 → 不动 task ───
log ""; log "=== 9. CAS CONCURRENT(current_stage≠l2_binding → 不动) ==="
RUN9=b4c1-concurrent-$TS; BR9=fix/$RUN9-extra
mkrun "$RUN9"
PR9=$(create_fix_pr "$BR9" "disc-conc-$TS.md" "conc")
if [ -z "$PR9" ]; then bad "CONCURRENT: PR 创建失败"; else
  # 模拟另一 Controller 已推进阶段
  PSQL "UPDATE task_runs SET current_stage='l2_awaiting_ticket' WHERE run_id='$RUN9';" >/dev/null
  ATT_BEFORE=$(PSQL "SELECT l2_discovery_attempts FROM task_runs WHERE run_id='$RUN9';")
  D9=$(discover "$RUN9")
  ST9=$(PSQL "SELECT status FROM task_runs WHERE run_id='$RUN9';")
  CS9=$(PSQL "SELECT current_stage FROM task_runs WHERE run_id='$RUN9';")
  B9=$(PSQL "SELECT count(*) FROM run_pr_bindings WHERE run_id='$RUN9';")
  logf "  discover: $(echo "$D9" | grep '^STATUS=' | head -1)"
  echo "$D9" | grep -q "^STATUS=CONCURRENT" && ok "阶段已改 → CONCURRENT(不写)" || bad "应 CONCURRENT: $(echo "$D9"|head -1)"
  [ "$ST9" = "APPROVAL_PENDING" ] && [ "$CS9" = "l2_awaiting_ticket" ] && ok "task 未被改动(仍 APPROVAL_PENDING/l2_awaiting_ticket)" || bad "task 被改: $ST9/$CS9"
  [ "$B9" = "0" ] && ok "未写 binding(CAS 失败不写)" || bad "CONCURRENT 却写了 binding: $B9"
fi

# ─── 10. 凭证扫描(全 evidence 含 8/9 输出)+ GitHub 残留记录 ───
log ""; log "=== 10. 凭证扫描 + GitHub 残留记录 ==="
set +e; grep -rniE "token=[A-Za-z0-9]{8}|Bearer [A-Za-z0-9]{8}|sk-live|access_token" "$EV" > "$EV/credential-scan.txt" 2>/dev/null; GR=$?; set -e
[ "$GR" -ne 0 ] && { : > "$EV/credential-scan.txt"; ok "无凭证泄漏"; } || bad "凭证泄漏(见 credential-scan.txt)"
log "  GitHub 残留(一次性 fix/b4c1-* 分支+PR,待具备 close 权限后清理): 记录于 github-residue.txt"
{ echo "## GitHub 一次性测试残留(待清理)";
  GW fixer --call list_branches owner=nghqqa repo=MergePilot 2>&1 | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); [print(' ',b['name']) for b in d if b.get('name','').startswith('fix/b4c1-')]" 2>/dev/null; } > "$EV/github-residue.txt" || true

PSQL "SELECT run_id,binding_id,pr_number,fix_branch,base_branch,substring(head_sha,1,12) FROM run_pr_bindings WHERE run_id LIKE 'b4c1-%' ORDER BY run_id;" > "$EV/bindings-snapshot.txt" 2>/dev/null
cleanup_runs
trap - EXIT
log ""
log "═══════════════════════════════════════════════"
log "  B4c-1.2 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
# 确保 controller 恢复运行
docker start "$NAME" >/dev/null 2>&1 || true
[ "$FAIL" -eq 0 ] || exit 1
