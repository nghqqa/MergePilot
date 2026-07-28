#!/bin/bash
# m3b-step2-fixture-isolation.sh — Step 2(fixture 隔离)验收。
# 硬验收:
#   A. 生产保护门:目标=nghqqa/MergePilot → e2e_guard 在任何 GitHub 写之前拒绝(exit 2);ALLOW_PRODUCTION_E2E=1 才放行。
#   C. 纵深防御:测试 Gateway(policy-gw-e2e)的 fixture-only policy 拒生产仓 create_branch(REPO_NOT_ALLOWED)。
#   B. fixture 全链:经测试 Gateway 完成 create_branch / create_file / create_pr / L2 merge / L2 close / 残留清理。
#      其中 merge/close 走 coordinator + 审批票据(经 l2_create_ticket/l2_approve/l2_claim_ticket),证明
#      fixture + 测试 Gateway + 审计库整链可用。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4c/step2-fixture
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out
OUT="$EV/step2-test.out"; : > "$OUT"
log(){ echo "$*" | tee -a "$OUT"; }
logf(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ log "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ log "  ❌ $1"; FAIL=$((FAIL+1)); }

# DB 超管(供 mkrun/binding/ticket/cleanup;走 audit-pg)
CTRL=/home/ngh/.config/mergepilot/controller.env
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=${PG_DB:-mergepilot_audit}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
# args_hash(与 gateway canonical_args_hash 对 merge/close payload 一致:无 approval_ticket,原生 JSON 类型)
ah(){ python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }
TS=$$

log "═══════════════════════════════════════════════"
log "  Step 2 fixture 隔离验收(target=$(e2e_repo) gw=$E2E_GW)"
log "═══════════════════════════════════════════════"

# ── 前置:起测试 Gateway ──
log "=== 前置:起测试 Gateway ==="
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "汇总: PASS=$PASS FAIL=$FAIL"; exit 1; }
if e2e_GW fixer --call list_pull_requests owner="$E2E_OWNER" repo="$E2E_REPO" state=open perPage=5 page=1 >>"$OUT" 2>&1; then
  ok "测试 Gateway 可达 fixture(list_pull_requests OK)"
else
  bad "测试 Gateway 不可达 fixture"; log "汇总: PASS=$PASS FAIL=$FAIL"; exit 1
fi

# ════════════ A. 生产保护门(脚本层)════════════
log ""; log "=== A. 生产保护门 e2e_guard(目标=生产仓 → 写前拒绝) ==="
GUARD_RC=$(E2E_REPO=MergePilot bash -c 'source /mnt/d/goai/mergepilot-os/tools/e2e-lib.sh; e2e_guard' 2>>"$OUT"; echo $?)
[ "$GUARD_RC" = "2" ] && ok "e2e_guard 拒生产目标(exit 2,任何 GitHub 写之前)" || bad "guard 未拒: rc=$GUARD_RC"
ALLOW_RC=$(ALLOW_PRODUCTION_E2E=1 E2E_REPO=MergePilot bash -c 'source /mnt/d/goai/mergepilot-os/tools/e2e-lib.sh; e2e_guard' 2>>"$OUT"; echo $?)
[ "$ALLOW_RC" = "0" ] && ok "ALLOW_PRODUCTION_E2E=1 显式放行(留 WARN 痕)" || bad "显式放行失败: rc=$ALLOW_RC"
# guard 默认(=fixture)放行
DEF_RC=$(bash -c 'source /mnt/d/goai/mergepilot-os/tools/e2e-lib.sh; e2e_guard' 2>>"$OUT"; echo $?)
[ "$DEF_RC" = "0" ] && ok "默认目标=fixture,guard 放行" || bad "默认 guard 误拒: rc=$DEF_RC"

# ════════════ C. 纵深防御(测试 Gateway 拒生产仓)════════════
log ""; log "=== C. 纵深防御:测试 Gateway fixture-only policy 拒生产仓 ==="
DENY=$(e2e_GW fixer --call create_branch owner=nghqqa repo=MergePilot branch=must-not-exist-$TS from_branch=main 2>&1)
echo "$DENY" >>"$OUT"
echo "$DENY" | grep -qiE "REPO_NOT_ALLOWED|POLICY_DENIED" && ok "测试 Gateway 拒生产仓 create_branch(REPO_NOT_ALLOWED)" || bad "测试 Gateway 未拒生产: ${DENY:0:80}"
# 验证生产仓未出现该分支(经**生产** gateway 读 MergePilot;测试 gateway 也拒生产读)
BR_EXISTS=$(docker exec policy-gw python3 /tmp/probe-tools.py fixer --call list_branches owner=nghqqa repo=MergePilot perPage=100 page=1 2>/dev/null | grep -c "must-not-exist-$TS" || true)
[ "$BR_EXISTS" = "0" ] && ok "生产仓未创建该分支(零写)" || bad "生产仓出现测试分支!"

# ════════════ B. fixture 全链 ═════════════
log ""; log "=== B. fixture 全链(branch/file/PR/merge/close/cleanup)==="
TS=$$
cleanup_fixture(){ # 清共享审计库的 step2- 行(fixture 仓库为 scratch,合并/分支残留可留)
  PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'step2-%'; DELETE FROM approvals WHERE run_id LIKE 'step2-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'step2-%'; DELETE FROM task_runs WHERE run_id LIKE 'step2-%';" >/dev/null 2>&1 || true
}
trap cleanup_fixture EXIT

create_fix_pr(){ local BR="$1" FILE="$2" R
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" 2>&1 | grep -qi ref && logf "  分支 $BR 建好"
  e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="$FILE" branch="$BR" content="step2-$TS" message="step2" 2>&1 | grep -qi "commit\|sha" && logf "  文件 $FILE 建好"
  R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="Step2 $3" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
read_head_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['head']['sha'])" 2>/dev/null; }
setup_l2_ticket(){ local RUN="$1" BR="$2" PR="$3" ACTION="$4" PAY="$5" BID TKT
  local HSHA; HSHA=$(read_head_sha "$PR"); [ -z "$HSHA" ] && { echo ""; return; }
  PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','$(e2e_repo)',$PR,'l2_awaiting_approval',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_approval';" >/dev/null
  BID="bnd-step2-$RUN"; TKT="tkt-step2-$RUN"
  PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','$E2E_BASE_BRANCH','$HSHA') ON CONFLICT (binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  local AH; AH=$(ah "$PAY")
  TKT=$(PSQL "SELECT l2_create_ticket('$BID','$ACTION','$PAY'::jsonb,'$AH',24,1);")
  PSQL "SELECT l2_approve('$TKT','step2@e2e');" >/dev/null
  echo "$TKT"; }

# B1. merge 链
BR_M=fix/step2-merge-$TS
PR_M=$(create_fix_pr "$BR_M" "step2-merge-$TS.md" "merge")
if [ -z "$PR_M" ]; then bad "fixture merge: 建分支/文件/PR 失败"; else
  ok "fixture: create_branch + create_file + create_pr(#$PR_M)"
  PAY_M='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR_M',"commit_title":"step2 merge","merge_method":"squash"}'
  TKT_M=$(setup_l2_ticket "step2-merge-$TS" "$BR_M" "$PR_M" "merge" "$PAY_M")
  if [ -z "$TKT_M" ]; then bad "fixture merge: 建票失败"; else
    e2e_GW coordinator --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR_M" commit_title="step2 merge" merge_method=squash approval_ticket="$TKT_M" >>"$OUT" 2>&1
    MERGED=$(e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR_M" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin).get('merged'))" 2>/dev/null)
    [ "$MERGED" = "True" ] && ok "fixture: L2 merge(经审批票据)→ PR #$PR_M merged" || bad "merge 未成功: merged=$MERGED"
    AST_M=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT_M';")
    [ "$AST_M" = "USED" ] && ok "approval → USED(整链收敛)" || bad "approval 应 USED: $AST_M"
  fi
fi

# B2. close 链
BR_C=fix/step2-close-$TS
PR_C=$(create_fix_pr "$BR_C" "step2-close-$TS.md" "close")
if [ -z "$PR_C" ]; then bad "fixture close: 建分支/文件/PR 失败"; else
  ok "fixture: create_branch + create_file + create_pr(#$PR_C)"
  PAY_C='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR_C',"state":"closed"}'
  TKT_C=$(setup_l2_ticket "step2-close-$TS" "$BR_C" "$PR_C" "close" "$PAY_C")
  if [ -z "$TKT_C" ]; then bad "fixture close: 建票失败"; else
    e2e_GW coordinator --call update_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR_C" state=closed approval_ticket="$TKT_C" >>"$OUT" 2>&1
    STATE_C=$(e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR_C" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin).get('state'))" 2>/dev/null)
    [ "$STATE_C" = "closed" ] && ok "fixture: L2 close(经审批票据)→ PR #$PR_C closed(未 merged)" || bad "close 未成功: state=$STATE_C"
    AST_C=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT_C';")
    [ "$AST_C" = "USED" ] && ok "close approval → USED" || bad "close approval 应 USED: $AST_C"
  fi
fi

# B3. cleanup
log ""; log "=== B3. 残留清理 ==="
cleanup_fixture; trap - EXIT
REMAIN=$(PSQL "SELECT count(*) FROM task_runs WHERE run_id LIKE 'step2-%';")
[ "$REMAIN" = "0" ] && ok "fixture DB 残留已清(task_runs=0)" || bad "DB 残留: $REMAIN"

# 凭证扫描
set +e; grep -rniE "token=[A-Za-z0-9]{8}|Bearer [A-Za-z0-9]{8}|sk-live|access_token" "$EV" > "$EV/credential-scan.txt" 2>/dev/null; GR=$?
[ "$GR" -ne 0 ] && { : > "$EV/credential-scan.txt"; ok "无凭证泄漏"; } || bad "凭证泄漏"

log ""
log "═══════════════════════════════════════════════"
log "  Step 2 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
