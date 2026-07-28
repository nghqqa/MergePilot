#!/bin/bash
# m3b-b4d-approve.sh — B4d approve CLI 验收(fixture 隔离)。
# 覆盖:
#   1. list  → 列 PENDING 票据(经 l2_pending_list)。
#   2. show  → 单票据详情(repo/PR/head SHA/action/TTL 剩余)。
#   3. approve → PENDING→APPROVED;approved_by = id -un@hostname(**不可参数伪造**)。
#   4. 拒绝:重复审批(已 APPROVED)/ 过期(approval_expires_at<=now)/ 不存在 → l2_approve FALSE。
#   5. 边界:approver 无表 SELECT/INSERT(仅 EXECUTE 2 函数)。
#   6. 凭证:approve.sh 输出不含密码/令牌。
# 所有票据建在 fixture 仓(nghqqa/MergePilot-e2e-fixture),经测试 Gateway;approve CLI 连 audit-pg。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b4d
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out
OUT="$EV/b4d-test.out"; : > "$OUT"
log(){ echo "$*" | tee -a "$OUT"; }
logf(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ log "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ log "  ❌ $1"; FAIL=$((FAIL+1)); }
TS=$$

# 超管(建票;approve CLI 不用超管,只用 approver)
CTRL=/home/ngh/.config/mergepilot/controller.env
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=${PG_DB:-mergepilot_audit}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
ah(){ python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }
APPROVE="$TOOLS/approve.sh"
HOST_BY="$(id -un)@$(hostname)"

cleanup(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4d-%'; DELETE FROM approvals WHERE run_id LIKE 'b4d-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4d-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4d-%';" >/dev/null 2>&1 || true; }
trap '{ cleanup; docker rm -f policy-gw-e2e 2>/dev/null; } EXIT'

log "═══════════════════════════════════════════════"
log "  B4d approve CLI 验收(fixture=$(e2e_repo); approve host-by=$HOST_BY)"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done

# 起测试 Gateway(建 fixture PR 用)
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "汇总: PASS=$PASS FAIL=$FAIL"; exit 1; }

create_fix_pr(){ local BR="$1" R
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" 2>&1 | grep -qi ref && logf "  分支 $BR 建好"
  e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="b4d-$TS.md" branch="$BR" content="b4d $TS" message="b4d" 2>&1 | grep -qi "commit\|sha" && logf "  commit 加好"
  R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="B4d $2" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
read_head_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null; }
# 在 fixture 建一张 PENDING merge 票;返 ticket_id。$1=run $2=branch $3=pr $4=label
mk_pending(){ local RUN="$1" BR="$2" PR="$3" HSHA BID PAY AH
  HSHA=$(read_head_sha "$PR"); [ -z "$HSHA" ] && { echo ""; return; }
  PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','$(e2e_repo)',$PR,'l2_awaiting_approval',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_approval';" >/dev/null
  BID="bnd-b4d-$RUN"
  PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','$E2E_BASE_BRANCH','$HSHA') ON CONFLICT (binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  PAY='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR',"commit_title":"b4d '"$4"'","merge_method":"squash"}'
  AH=$(ah "$PAY")
  PSQL "SELECT l2_create_ticket('$BID','merge','$PAY'::jsonb,'$AH',24,1);"; }
cleanup

# ─── 1. list / show / approve 正向 ───
log ""; log "=== 1. list / show / approve 正向 ==="
RUN1=b4d-ok-$TS; BR1=fix/b4d-ok-$TS
PR1=$(create_fix_pr "$BR1" "ok")
if [ -z "$PR1" ]; then bad "正向: fixture PR 建失败"; else
  TKT1=$(mk_pending "$RUN1" "$BR1" "$PR1" "ok")
  if [ -z "$TKT1" ]; then bad "正向: 建票失败"; else
    # list 含该票
    if bash "$APPROVE" list 2>>"$OUT" | grep -q "$TKT1"; then ok "list 列出 PENDING 票 $TKT1"; else bad "list 未列出该票"; fi
    # show 详情
    SHOW_OUT=$(bash "$APPROVE" show "$TKT1" 2>>"$OUT")
    echo "$SHOW_OUT" >>"$OUT"
    echo "$SHOW_OUT" | grep -q "$(e2e_repo)" && echo "$SHOW_OUT" | grep -q "PR:" && echo "$SHOW_OUT" | grep -q "head SHA:" && echo "$SHOW_OUT" | grep -q "TTL:" \
      && ok "show 展示 repo/PR/head SHA/TTL" || bad "show 字段缺失"
    # approve → APPROVED
    APR_OUT=$(bash "$APPROVE" approve "$TKT1" 2>>"$OUT"); echo "$APR_OUT" >>"$OUT"
    echo "$APR_OUT" | grep -q "✓ APPROVED" && ok "approve → APPROVED" || bad "approve 未成功: $APR_OUT"
    ST1=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
    BY1=$(PSQL "SELECT approved_by FROM approvals WHERE ticket_id='$TKT1';")
    [ "$ST1" = "APPROVED" ] && ok "DB: 票状态 APPROVED" || bad "DB 状态异常: $ST1"
    [ "$BY1" = "$HOST_BY" ] && ok "approved_by = $HOST_BY(host 派生,正确)" || bad "approved_by 异常: '$BY1' ≠ '$HOST_BY'"
    # expires_at 被设(approved_at + exec_ttl)
    EXP1=$(PSQL "SELECT expires_at IS NOT NULL AND expires_at > now() FROM approvals WHERE ticket_id='$TKT1';")
    [ "$EXP1" = "t" ] && ok "approve 写入执行期 expires_at(>now)" || bad "expires_at 异常"
  fi
fi

# ─── 2. approved_by 不可参数伪造 ───
log ""; log "=== 2. approved_by 不可参数伪造(CLI 无 --by 入口)==="
# 源码层:approved_by 必为硬派生(无 \$2..\$9 作 approved_by;无 --by 标志)
if grep -q 'APPROVED_BY="$(id -un)@$(hostname)"' "$APPROVE" && ! grep -qE 'approved_by[=: ].*\$[2-9]|[[:space:]]--by' "$APPROVE"; then
  ok "approve.sh: approved_by 硬派生自 id -un@hostname(无参数入口)"
else
  bad "approve.sh 似乎接受 approved_by 参数"
fi
# 实测:多传伪造参数 → approved_by 仍是 host 身份(用一张新票)
RUN2=b4d-forgery-$TS; BR2=fix/b4d-forgery-$TS
PR2=$(create_fix_pr "$BR2" "forgery")
TKT2=$(mk_pending "$RUN2" "$BR2" "$PR2" "forgery")
if [ -n "$TKT2" ]; then
  bash "$APPROVE" approve "$TKT2" "evil@forged-identity" --by=admin@fake 2>>"$OUT" | grep -q "✓ APPROVED" || true
  BY2=$(PSQL "SELECT approved_by FROM approvals WHERE ticket_id='$TKT2';")
  [ "$BY2" = "$HOST_BY" ] && ok "伪造参数被忽略,approved_by 仍是 $HOST_BY" || bad "伪造成功! approved_by='$BY2'"
fi

# ─── 3. 拒绝路径 ───
log ""; log "=== 3. 拒绝路径(l2_approve CAS FALSE)==="
# 3a. 重复审批(已 APPROVED 的 TKT1)
RC=$(bash "$APPROVE" approve "$TKT1" 2>>"$OUT" >/dev/null; echo $?)
[ "$RC" = "1" ] && ok "重复审批(已 APPROVED)→ 拒(exit 1)" || bad "重复审批未拒: rc=$RC"
# 3b. 过期(PENDING 但 approval_expires_at<=now)
RUN3=b4d-exp-$TS; BR3=fix/b4d-exp-$TS
PR3=$(create_fix_pr "$BR3" "exp")
TKT3=$(mk_pending "$RUN3" "$BR3" "$PR3" "exp")
if [ -n "$TKT3" ]; then
  PSQL "UPDATE approvals SET approval_expires_at = now() - interval '1 hour' WHERE ticket_id='$TKT3';" >/dev/null
  RC=$(bash "$APPROVE" approve "$TKT3" 2>>"$OUT" >/dev/null; echo $?)
  ST3=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT3';")
  [ "$RC" = "1" ] && [ "$ST3" = "PENDING" ] && ok "过期票 → 拒(状态仍 PENDING,未迁移)" || bad "过期票异常: rc=$RC status=$ST3"
fi
# 3c. 不存在
RC=$(bash "$APPROVE" approve "tkt-00000000-0000-0000-0000-000000000000" 2>>"$OUT" >/dev/null; echo $?)
[ "$RC" = "1" ] && ok "不存在的 ticket_id → 拒" || bad "不存在票未拒: rc=$RC"
# 3d. 非法 ticket_id 格式 → CLI 拒(exit 2)
RC=$(bash "$APPROVE" approve "not-a-ticket; DROP TABLE" 2>>"$OUT" >/dev/null; echo $?)
[ "$RC" = "2" ] && ok "非法 ticket_id 格式 → CLI 拒(exit 2,防注入)" || bad "非法格式未拒: rc=$RC"

# ─── 4. approver 权限边界(仅 EXECUTE 2 函数,无表 SELECT/INSERT)───
log ""; log "=== 4. approver 权限边界 ==="
APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' /home/ngh/.config/mergepilot/b4-roles.env | head -1 | cut -d= -f2-)
SEL=$(docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "SELECT count(*) FROM approvals;" 2>&1)
INS=$(docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "INSERT INTO approvals(ticket_id) VALUES('x');" 2>&1)
echo "$SEL" | grep -qi "permission denied" && ok "approver SELECT approvals → denied" || bad "approver 可 SELECT! $SEL"
echo "$INS" | grep -qi "permission denied" && ok "approver INSERT approvals → denied" || bad "approver 可 INSERT! $INS"
# approver 能调 l2_pending_list / l2_approve(已在正向间接验证;再显式确认 pending_list 可调)
PL=$(docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "SELECT count(*) FROM l2_pending_list();" 2>&1)
[[ "$PL" =~ ^[0-9]+$ ]] && ok "approver EXECUTE l2_pending_list → OK(返回计数 $PL)" || bad "pending_list 不可调: $PL"

# ─── 5. 凭证扫描(approve.sh 输出不含密码/令牌)───
log ""; log "=== 5. 凭证扫描 ==="
bash "$APPROVE" list > "$EV/list-out.txt" 2>&1
[ -n "${TKT3:-}" ] && bash "$APPROVE" show "$TKT3" > "$EV/show-out.txt" 2>&1 || true
LEAK=0
# 精确:输出不得含 approver 密码串,也不得出现 PGPASSWORD/PASS 等字样
grep -F "$APV_PW" "$EV/list-out.txt" "$EV/show-out.txt" >/dev/null 2>&1 && LEAK=1
grep -iE "PGPASSWORD|APPROVER_PASS|MERGEPILOT_APPROVER_PASS|password|passwd" "$EV/list-out.txt" "$EV/show-out.txt" >/dev/null 2>&1 && LEAK=1
if [ "$LEAK" = "0" ]; then
  { echo "approve.sh list/show 输出扫描:不含 approver 密码,无 PGPASSWORD/PASS 字样。"; } > "$EV/credential-scan.txt"
  ok "approve.sh 输出无凭证泄漏"
else
  { echo "!!! 可能泄漏:"; grep -nE "PGPASSWORD|APPROVER_PASS|password|$APV_PW" "$EV/list-out.txt" "$EV/show-out.txt" 2>/dev/null | head; } > "$EV/credential-scan.txt"
  bad "approve.sh 输出疑似含凭证(见 credential-scan.txt)"
fi

cleanup
trap - EXIT
log ""
log "═══════════════════════════════════════════════"
log "  B4d 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
