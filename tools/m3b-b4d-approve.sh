#!/bin/bash
# m3b-b4d-approve.sh — B4d/B4d.1 approve CLI 验收(fixture 隔离)。
# B4d.1 hardening 覆盖(针对 B4d 复审):
#   P1 身份:l2_approve 用 session_user(B4d.1),approved_by 不可伪造 —— 直调 l2_approve('tkt','EVIL') 仍记 session_user;
#           逐人身份 = 逐人 DB 登录(第二角色 mergepilot_approver_alt 演示)。
#   P1 测试:缺票显式 bad(不静默跳过);结尾 [ PASS -eq N ] && [ FAIL -eq 0 ]。
#   P2 参数:approve 多余参数 → exit 2,票保持 PENDING。
#   P2 fixture:trap 清 fixture 的 B4d PR/分支(不污染 scratch fixture)。
# 基线覆盖(B4d):list/show/approve 正向 + 拒绝路径 + approver 权限边界 + 凭证扫描。
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

CTRL=/home/ngh/.config/mergepilot/controller.env
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_SU=${PG_SU:-mergepilot}
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=${PG_DB:-mergepilot_audit}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' /home/ngh/.config/mergepilot/b4-roles.env | head -1 | cut -d= -f2-)
ah(){ python3 -c "import hashlib,json,sys;d=json.loads(sys.argv[1]);print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }
APPROVE="$TOOLS/approve.sh"

cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4d-%'; DELETE FROM approvals WHERE run_id LIKE 'b4d-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4d-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4d-%';" >/dev/null 2>&1 || true; }
cleanup_fixture(){ # 关 fixture 上所有 open B4d PR + 删其分支(含历史残留)。WSL 内 gh 不在,经 gh.exe interop。
  for n in $(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 100 --json number,title -q '.[] | select(.title|test("B4d")) | .number' 2>/dev/null); do
    gh.exe pr close "$n" --repo "$(e2e_repo)" --delete-branch --comment "B4d 测试清理" >/dev/null 2>&1 || true
  done; }
cleanup_role(){ PSQL "REVOKE EXECUTE ON FUNCTION l2_approve(text,text) FROM mergepilot_approver_alt;" >/dev/null 2>&1 || true
  docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "DROP OWNED BY mergepilot_approver_alt; DROP ROLE IF EXISTS mergepilot_approver_alt;" >/dev/null 2>&1 || true; }
trap '{ cleanup_db; cleanup_role; cleanup_fixture; docker rm -f policy-gw-e2e 2>/dev/null; } EXIT'

log "═══════════════════════════════════════════════"
log "  B4d.1 approve CLI 验收(fixture=$(e2e_repo))"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done

# 应用 B4d.1 hardening 迁移(幂等)+ 起测试 Gateway
docker cp "$TOOLS/audit-db/m3b_b4d1.sql" audit-pg:/tmp/m3b_b4d1.sql >/dev/null
docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 -f /tmp/m3b_b4d1.sql >>"$OUT" 2>&1 || bad "m3b_b4d1.sql 应用失败"
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "汇总: PASS=$PASS FAIL=$FAIL"; exit 1; }
cleanup_db; cleanup_fixture

create_fix_pr(){ local BR="$1" R
  e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" 2>&1 | grep -qi ref && logf "  分支 $BR 建好"
  e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="b4d-$TS.md" branch="$BR" content="b4d $TS" message="b4d" 2>&1 | grep -qi "commit\|sha" && logf "  commit 加好"
  R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="B4d $2" body=auto 2>&1 || true)
  echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1; }
read_head_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null; }
mk_pending(){ local RUN="$1" BR="$2" PR="$3" HSHA BID PAY AH
  HSHA=$(read_head_sha "$PR"); [ -z "$HSHA" ] && { echo ""; return; }
  PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','$(e2e_repo)',$PR,'l2_awaiting_approval',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_approval';" >/dev/null
  BID="bnd-b4d-$RUN"
  PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','$E2E_BASE_BRANCH','$HSHA') ON CONFLICT (binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  PAY='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR',"commit_title":"b4d '"$4"'","merge_method":"squash"}'
  AH=$(ah "$PAY")
  PSQL "SELECT l2_create_ticket('$BID','merge','$PAY'::jsonb,'$AH',24,1);"; }

# ─── 1. list / show / approve 正向(session_user 身份)───
log ""; log "=== 1. list / show / approve 正向 ==="
RUN1=b4d-ok-$TS; BR1=fix/b4d-ok-$TS
PR1=$(create_fix_pr "$BR1" "ok")
TKT1=$(mk_pending "$RUN1" "$BR1" "$PR1" "ok")
[ -z "$TKT1" ] && bad "正向: fixture PR/票建失败(显式失败,不跳过)"
if [ -n "$TKT1" ]; then
  bash "$APPROVE" list 2>>"$OUT" | grep -q "$TKT1" && ok "list 列出 PENDING 票" || bad "list 未列出该票"
  SHOW_OUT=$(bash "$APPROVE" show "$TKT1" 2>>"$OUT"); echo "$SHOW_OUT" >>"$OUT"
  echo "$SHOW_OUT" | grep -q "$(e2e_repo)" && echo "$SHOW_OUT" | grep -q "approvable: yes" \
    && ok "show 展示 repo + approvable=yes(PENDING 未过期)" || bad "show 字段/approvable 异常"
  APR_OUT=$(bash "$APPROVE" approve "$TKT1" 2>>"$OUT"); echo "$APR_OUT" >>"$OUT"
  echo "$APR_OUT" | grep -q "✓ APPROVED" && ok "approve → APPROVED" || bad "approve 未成功"
  ST1=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
  BY1=$(PSQL "SELECT approved_by FROM approvals WHERE ticket_id='$TKT1';")
  [ "$ST1" = "APPROVED" ] && ok "DB: 票状态 APPROVED" || bad "DB 状态异常: $ST1"
  [ "$BY1" = "mergepilot_approver" ] && ok "approved_by = mergepilot_approver(session_user,非参数)" || bad "approved_by 异常: '$BY1'"
  PSQL "SELECT expires_at > now() FROM approvals WHERE ticket_id='$TKT1';" | grep -q t && ok "approve 写入执行期 expires_at(>now)" || bad "expires_at 异常"
fi

# ─── 2. 身份不可伪造(session_user 权威)───
log ""; log "=== 2. approved_by 不可伪造(session_user 权威)==="
# 注:静态源码 grep 易因注释里的 "--by" 误判;非伪造性以 2b(伪造参数被忽略)+ §4(严格参数拒)功能证明为准。
# 2b. 直调 l2_approve 带"伪造"2nd arg → 仍记 session_user(函数忽略参数)
RUN2=b4d-forgery-$TS; BR2=fix/b4d-forgery-$TS
PR2=$(create_fix_pr "$BR2" "forgery")
TKT2=$(mk_pending "$RUN2" "$BR2" "$PR2" "forgery")
[ -z "$TKT2" ] && bad "伪造用例: 票建失败(显式失败)"
if [ -n "$TKT2" ]; then
  docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A \
    -c "SELECT l2_approve('$TKT2','EVIL@FORGED-IDENTITY')::text;" >>"$OUT" 2>&1
  BY2=$(PSQL "SELECT approved_by FROM approvals WHERE ticket_id='$TKT2';")
  [ "$BY2" = "mergepilot_approver" ] && ok "直调 l2_approve('tkt','EVIL@FORGED') → approved_by 仍为 session_user(参数被忽略)" || bad "伪造成功! approved_by='$BY2'"
fi
# 2c. 逐人身份:第二角色 mergepilot_approver_alt 审批 → approved_by = 该角色
PSQL "CREATE ROLE mergepilot_approver_alt LOGIN PASSWORD 'alt-b4d1-test'; GRANT EXECUTE ON FUNCTION l2_approve(text,text) TO mergepilot_approver_alt;" >/dev/null
RUN2c=b4d-alt-$TS; BR2c=fix/b4d-alt-$TS
PR2c=$(create_fix_pr "$BR2c" "alt")
TKT2c=$(mk_pending "$RUN2c" "$BR2c" "$PR2c" "alt")
[ -z "$TKT2c" ] && bad "逐人用例: 票建失败(显式失败)"
if [ -n "$TKT2c" ]; then
  docker exec -e PGPASSWORD="alt-b4d1-test" audit-pg psql -U mergepilot_approver_alt -d "$PG_DB" -t -A \
    -c "SELECT l2_approve('$TKT2c')::text;" >>"$OUT" 2>&1
  BY2c=$(PSQL "SELECT approved_by FROM approvals WHERE ticket_id='$TKT2c';")
  [ "$BY2c" = "mergepilot_approver_alt" ] && ok "逐人 DB 登录:第二角色审批 → approved_by=mergepilot_approver_alt" || bad "逐人身份异常: '$BY2c'"
fi

# ─── 3. 拒绝路径(l2_approve CAS FALSE)───
log ""; log "=== 3. 拒绝路径 ==="
if [ -n "${TKT1:-}" ]; then
  RC=$(bash "$APPROVE" approve "$TKT1" 2>>"$OUT" >/dev/null; echo $?)
  [ "$RC" = "1" ] && ok "重复审批(已 APPROVED)→ 拒(exit 1)" || bad "重复审批未拒: rc=$RC"
fi
RUN3=b4d-exp-$TS; BR3=fix/b4d-exp-$TS
PR3=$(create_fix_pr "$BR3" "exp")
TKT3=$(mk_pending "$RUN3" "$BR3" "$PR3" "exp")
[ -z "$TKT3" ] && bad "过期用例: 票建失败(显式失败)"
if [ -n "$TKT3" ]; then
  PSQL "UPDATE approvals SET approval_expires_at = now() - interval '1 hour' WHERE ticket_id='$TKT3';" >/dev/null
  RC=$(bash "$APPROVE" approve "$TKT3" 2>>"$OUT" >/dev/null; echo $?)
  ST3=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT3';")
  [ "$RC" = "1" ] && [ "$ST3" = "PENDING" ] && ok "过期票 → 拒(状态仍 PENDING)" || bad "过期票异常: rc=$RC status=$ST3"
  # list 标记过期票为 EXPIRED(不可审批)
  bash "$APPROVE" list 2>>"$OUT" | grep -q "EXPIRED" && ok "list 把过期 PENDING 票标 EXPIRED(不可审批)" || bad "list 未标记 EXPIRED"
fi
RC=$(bash "$APPROVE" approve "tkt-00000000-0000-0000-0000-000000000000" 2>>"$OUT" >/dev/null; echo $?)
[ "$RC" = "1" ] && ok "不存在的 ticket_id → 拒" || bad "不存在票未拒: rc=$RC"
RC=$(bash "$APPROVE" approve "not-a-ticket" 2>>"$OUT" >/dev/null; echo $?)
[ "$RC" = "2" ] && ok "非法 ticket_id 格式 → CLI exit 2(防注入)" || bad "非法格式未拒: rc=$RC"

# ─── 4. 严格参数(P2):多余参数 → exit 2,票保持 PENDING ───
log ""; log "=== 4. 严格参数(多余参数 → exit 2,票 PENDING)==="
RUN4=b4d-strictarg-$TS; BR4=fix/b4d-strictarg-$TS
PR4=$(create_fix_pr "$BR4" "strictarg")
TKT4=$(mk_pending "$RUN4" "$BR4" "$PR4" "strictarg")
[ -z "$TKT4" ] && bad "严格参数用例: 票建失败(显式失败)"
if [ -n "$TKT4" ]; then
  RC=$(bash "$APPROVE" approve "$TKT4" --by=admin@fake 2>>"$OUT" >/dev/null; echo $?)
  ST4=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT4';")
  [ "$RC" = "2" ] && [ "$ST4" = "PENDING" ] && ok "approve <tkt> --by=... → exit 2 且票保持 PENDING" || bad "严格参数异常: rc=$RC status=$ST4"
fi

# ─── 5. approver 权限边界 ───
log ""; log "=== 5. approver 权限边界 ==="
SEL=$(docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "SELECT count(*) FROM approvals;" 2>&1)
INS=$(docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "INSERT INTO approvals(ticket_id) VALUES('x');" 2>&1)
echo "$SEL" | grep -qi "permission denied" && ok "approver SELECT approvals → denied" || bad "approver 可 SELECT!"
echo "$INS" | grep -qi "permission denied" && ok "approver INSERT approvals → denied" || bad "approver 可 INSERT!"
PL=$(docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "SELECT count(*) FROM l2_pending_list();" 2>&1)
[[ "$PL" =~ ^[0-9]+$ ]] && ok "approver EXECUTE l2_pending_list → OK(计数 $PL)" || bad "pending_list 异常: $PL"

# ─── 6. 凭证扫描 ───
log ""; log "=== 6. 凭证扫描 ==="
bash "$APPROVE" list > "$EV/list-out.txt" 2>&1
[ -n "${TKT3:-}" ] && bash "$APPROVE" show "$TKT3" > "$EV/show-out.txt" 2>&1 || true
LEAK=0
grep -F "$APV_PW" "$EV/list-out.txt" "$EV/show-out.txt" >/dev/null 2>&1 && LEAK=1
grep -iE "PGPASSWORD|APPROVER_PASS|MERGEPILOT_APPROVER_PASS|password|passwd" "$EV/list-out.txt" "$EV/show-out.txt" >/dev/null 2>&1 && LEAK=1
if [ "$LEAK" = "0" ]; then
  { echo "approve.sh list/show 输出扫描:不含 approver 密码,无 PGPASSWORD/PASS 字样。"; } > "$EV/credential-scan.txt"
  ok "approve.sh 输出无凭证泄漏"
else
  { echo "!!! 可能泄漏:"; grep -nEi "PGPASSWORD|APPROVER_PASS|password|$APV_PW" "$EV/list-out.txt" "$EV/show-out.txt" 2>/dev/null | head; } > "$EV/credential-scan.txt"
  bad "approve.sh 输出疑似含凭证"
fi

cleanup_db; cleanup_role; cleanup_fixture
# 退出时剥证据文件尾随空白(psql 表格输出带尾空格,致 git diff --check 报错)
trap 'sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$EV"/*.out 2>/dev/null' EXIT
log ""
log "═══════════════════════════════════════════════"
log "  B4d.1 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
# P1 hardening:必须跑满且全过(防"少跑仍绿")。N = 下方断言数(见各 ok/bad)。
EXPECTED_PASS=18
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED_PASS" ]; then
  log "  全部 $EXPECTED_PASS 项断言通过(无静默跳过)"; exit 0
else
  log "  失败或未跑满(期望 $EXPECTED_PASS,实际 PASS=$PASS FAIL=$FAIL)"; exit 1
fi
