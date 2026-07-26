#!/bin/bash
# m3b-b2-test.sh — B2 + B2.1 + B2.2 全量策略验证。
# 探针(mcp client 直连 gateway)精确判定。全部 DENY 或无副作用读/搜索。
# B2.2 重点:搜索逃逸(拒用户 scope,gateway 注入)+ 残留过权禁用 + 测试加固。
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-b2-test.sh
# 退出码:全过 0,否则 1(CI/证据收集不可接受 FAIL)
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b2-test.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

PV=$(docker logs policy-gw 2>&1 | grep -aoE "policy_version=[a-z0-9.-]+" | tail -1 | cut -d= -f2)
PH=$(docker logs policy-gw 2>&1 | grep -aoE "policy_hash=[a-f0-9]+" | tail -1 | cut -d= -f2)
log "═══════════════════════════════════════════════"
log "  B2+B2.1+B2.2 验证 (policy=$PV hash=$PH)"
log "═══════════════════════════════════════════════"
# 测试起点时间戳(审计只计本轮,不累计历史)
START_TS=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c "SELECT now();" 2>/dev/null)

docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py 2>/dev/null
probe_list(){ docker exec policy-gw python3 /tmp/probe-tools.py "$1" 2>/dev/null; }
probe_call(){ docker exec policy-gw python3 /tmp/probe-tools.py "$1" --call "${@:2}" 2>/dev/null; }
has(){ echo "$1" | python3 -c "import sys,json;print('$2' in json.load(sys.stdin))"; }
deny_is(){ echo "$1" | grep -qiE "POLICY_DENIED.*$2"; }
allowed(){ echo "$1" | grep -qi POLICY_DENIED && echo False || echo True; }

DISABLED="create_repository fork_repository search_repositories search_users assign_copilot_to_issue issue_write sub_issue_write update_pull_request_branch get_teams get_team_members list_issue_fields list_issue_types"
disabled_visible(){ echo "$1" | python3 -c "
import sys,json
d=json.load(sys.stdin); dis='$DISABLED'.split()
print(sum(1 for t in dis if t in d))
"; }

# ─── 1. list 过滤 ───
log ""; log "=== A. reviewer list 无写/L2/disabled ==="
REV=$(probe_list reviewer); RN=$(echo "$REV"|python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
DV=$(disabled_visible "$REV")
log "  reviewer($RN): merge=$(has "$REV" merge_pull_request) create=$(has "$REV" create_branch) get_me=$(has "$REV" get_me) disabled可见=$DV"
[ "$(has "$REV" merge_pull_request)" = False ] && [ "$(has "$REV" create_branch)" = False ] && [ "$(has "$REV" get_me)" = True ] && ok "reviewer 无 merge/create_branch,有 get_me($RN)" || bad "reviewer 过滤异常"
[ "$DV" = 0 ] && ok "reviewer 无 disabled 工具可见" || bad "reviewer 可见 $DV 个 disabled"

log ""; log "=== B. reviewer get_me → ALLOW ==="
GM=$(probe_call reviewer get_me owner=nghqqa repo=MergePilot); echo "$GM"|head -1 >> "$OUT"
echo "$GM" | grep -qiE "login|nghqqa" && ok "reviewer 读成功" || bad "reviewer 读失败"

log ""; log "=== C. fixer list 有 create_branch+update_pull_request,无残留写/disabled ==="
FIX=$(probe_list fixer); FN=$(echo "$FIX"|python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
FDV=$(disabled_visible "$FIX")
log "  fixer($FN): create=$(has "$FIX" create_branch) upd=$(has "$FIX" update_pull_request) issue_write=$(has "$FIX" issue_write) upb=$(has "$FIX" update_pull_request_branch) disabled=$FDV"
[ "$(has "$FIX" create_branch)" = True ] && [ "$(has "$FIX" update_pull_request)" = True ] && [ "$(has "$FIX" issue_write)" = False ] && [ "$(has "$FIX" update_pull_request_branch)" = False ] && ok "fixer 有 create_branch+upd,无 issue_write/upb($FN)" || bad "fixer 残留过权"
[ "$FDV" = 0 ] && ok "fixer 无 disabled 可见" || bad "fixer 可见 $FD 个 disabled"

log ""; log "=== H. coordinator list 有 merge+upd,无 fix 类/disabled ==="
COORD=$(probe_list coordinator); CN=$(echo "$COORD"|python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
CDV=$(disabled_visible "$COORD")
log "  coordinator($CN): merge=$(has "$COORD" merge_pull_request) couf=$(has "$COORD" create_or_update_file) teams=$(has "$COORD" get_teams) disabled=$CDV"
[ "$(has "$COORD" merge_pull_request)" = True ] && [ "$(has "$COORD" create_or_update_file)" = False ] && [ "$(has "$COORD" get_teams)" = False ] && ok "coordinator 有 merge,无 fix 类/teams($CN)" || bad "coordinator 异常"
[ "$CDV" = 0 ] && ok "coordinator 无 disabled 可见" || bad "coordinator 可见 $CD 个 disabled"

# ─── 2. 写约束 ───
log ""; log "=== D. fixer create_branch(from_branch=evil) → BASE_NOT_ALLOWED [B2.1] ==="
D=$(probe_call fixer create_branch owner=nghqqa repo=MergePilot branch=fix/x from_branch=evil); echo "$D"|tail -1 >> "$OUT"
deny_is "$D" "BASE_NOT_ALLOWED" && ok "from_branch 被拒" || bad "from_branch 绕过"
log ""; log "=== E. fixer 写 main → BRANCH_PROTECTED ==="
E=$(probe_call fixer create_or_update_file owner=nghqqa repo=MergePilot path=README.md branch=main content=x message=x); echo "$E"|tail -1 >> "$OUT"
deny_is "$E" "BRANCH_PROTECTED" && ok "写 main 被拒" || bad "写 main 应拒"
log ""; log "=== F. fixer 写 .env → PATH_DENIED ==="
F=$(probe_call fixer create_or_update_file owner=nghqqa repo=MergePilot path=.env branch=fix/t content=x message=x); echo "$F"|tail -1 >> "$OUT"
deny_is "$F" "PATH_DENIED" && ok "写 .env 被拒" || bad "写 .env 应拒"
log ""; log "=== G. fixer 写非 allowlist repo → REPO_NOT_ALLOWED ==="
G=$(probe_call fixer create_or_update_file owner=evil repo=other path=x.txt branch=fix/t content=x message=x); echo "$G"|tail -1 >> "$OUT"
deny_is "$G" "REPO_NOT_ALLOWED" && ok "非 allowlist repo 被拒" || bad "非 allowlist 应拒"
log ""; log "=== J2. coordinator create_or_update_file → TOOL_NOT_ALLOWED [B2.1] ==="
J2=$(probe_call coordinator create_or_update_file owner=nghqqa repo=MergePilot path=x branch=fix/t content=x message=x); echo "$J2"|tail -1 >> "$OUT"
deny_is "$J2" "TOOL_NOT_ALLOWED" && ok "coordinator 无 fix 类工具" || bad "coordinator 过权"
log ""; log "=== K. fixer assign_copilot_to_issue → TOOL_NOT_ALLOWED ==="
K=$(probe_call fixer assign_copilot_to_issue owner=nghqqa repo=MergePilot issueNumber=1); echo "$K"|tail -1 >> "$OUT"
deny_is "$K" "TOOL_NOT_ALLOWED" && ok "assign_copilot 被拒" || bad "assign_copilot 应禁"
log ""; log "=== L. reviewer 读非 allowlist repo → REPO_NOT_ALLOWED [B2.1] ==="
L=$(probe_call reviewer get_file_contents owner=evil repo=other path=x); echo "$L"|tail -1 >> "$OUT"
deny_is "$L" "REPO_NOT_ALLOWED" && ok "reviewer 读非 allowlist 被拒" || bad "读未覆盖 allowlist"

# ─── 3. 搜索(B2.2 重点:拒用户 scope,gateway 注入)───
log ""; log "=== M. reviewer search_code 纯术语 → ALLOW(gateway 注入 repo scope)==="
M=$(probe_call reviewer search_code query="password"); echo "$M"|head -1 >> "$OUT"
[ "$(allowed "$M")" = True ] && ok "纯术语搜索放行(scope 由 gateway 注入)" || bad "纯术语应放行: $(echo "$M"|tail -1)"

log ""; log "=== M2. query 含 repo: 限定符 → SEARCH_QUALIFIER_FORBIDDEN [B2.2] ==="
M2=$(probe_call reviewer search_code query="repo:nghqqa/MergePilot password"); echo "$M2"|tail -1 >> "$OUT"
deny_is "$M2" "SEARCH_QUALIFIER_FORBIDDEN" && ok "用户自带 repo: 被拒" || bad "用户 scope 应拒(逃逸!)"

log ""; log "=== M3. repo:allowlist OR password → 拒 [B2.2 关键逃逸] ==="
M3=$(probe_call reviewer search_code query="repo:nghqqa/MergePilot OR password"); echo "$M3"|tail -1 >> "$OUT"
deny_is "$M3" "SEARCH_QUALIFIER_FORBIDDEN" && ok "布尔逃逸被拒(repo: + OR)" || bad "!!! 布尔逃逸成功 !!!"

log ""; log "=== M4. password OR secret(无 scope)→ SEARCH_OPERATOR_NOT_ALLOWED [B2.2] ==="
M4=$(probe_call reviewer search_code query="password OR secret"); echo "$M4"|tail -1 >> "$OUT"
deny_is "$M4" "SEARCH_OPERATOR_NOT_ALLOWED" && ok "OR 算子被拒" || bad "OR 应拒"

log ""; log "=== M5. repo:allowlist OR repo:evil/x → 拒 [B2.2] ==="
M5=$(probe_call reviewer search_code query="repo:nghqqa/MergePilot OR repo:evil/x"); echo "$M5"|tail -1 >> "$OUT"
deny_is "$M5" "SEARCH_QUALIFIER_FORBIDDEN" && ok "多 repo OR 被拒" || bad "多 repo OR 应拒"

# ─── 4. update_pull_request 字段白名单 ───
log ""; log "=== O. fixer update_pull_request(state=open) → L2_TICKET_REQUIRED ==="
O=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999 state=open); echo "$O"|tail -1 >> "$OUT"
deny_is "$O" "L2_TICKET_REQUIRED" && ok "state 被拒(L2)" || bad "state 应 L2 拒"
log ""; log "=== P. fixer update_pull_request(base=evil) → PR_FIELD_NOT_ALLOWED ==="
P=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999 base=evil); echo "$P"|tail -1 >> "$OUT"
deny_is "$P" "PR_FIELD_NOT_ALLOWED" && ok "base 字段被拒" || bad "base 应拒"
log ""; log "=== Q. fixer update_pull_request(draft=true) → PR_FIELD_NOT_ALLOWED ==="
Q=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999 draft=true); echo "$Q"|tail -1 >> "$OUT"
deny_is "$Q" "PR_FIELD_NOT_ALLOWED" && ok "draft 字段被拒" || bad "draft 应拒"
log ""; log "=== R. fixer update_pull_request(title=...) → ALLOW(字段白名单通过)==="
R=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999999 title=test-title); echo "$R"|tail -1 >> "$OUT"
[ "$(allowed "$R")" = True ] && ok "title 字段放行(GitHub 对不存在 PR 报错无副作用)" || bad "title 应放行"

# ─── 5. L2 ───
log ""; log "=== I. coordinator merge → L2_TICKET_REQUIRED ==="
I=$(probe_call coordinator merge_pull_request owner=nghqqa repo=MergePilot pullNumber=999); echo "$I"|tail -1 >> "$OUT"
deny_is "$I" "L2_TICKET_REQUIRED" && ok "coordinator merge 被拒(L2)" || bad "merge 应 L2 拒"

# ─── 6. 审计(本轮,按时间戳过滤)───
log ""; log "=== T. 审计 DENY(本轮 $START_TS 起)==="
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT reason_code,count(*) FROM mcp_calls WHERE decision='DENY' AND ts > '$START_TS' GROUP BY reason_code ORDER BY count(*) DESC;" 2>/dev/null >> "$OUT"
DENY_CNT=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE decision='DENY' AND ts > '$START_TS' AND reason_code IN ('BASE_NOT_ALLOWED','BRANCH_PROTECTED','PATH_DENIED','REPO_NOT_ALLOWED','L2_TICKET_REQUIRED','TOOL_NOT_ALLOWED','SEARCH_QUALIFIER_FORBIDDEN','SEARCH_OPERATOR_NOT_ALLOWED','PR_FIELD_NOT_ALLOWED');" 2>/dev/null)
log "  本轮策略 DENY: ${DENY_CNT}"
[ "${DENY_CNT:-0}" -ge 12 ] && ok "审计记录 $DENY_CNT 条本轮 DENY" || bad "本轮 DENY 不足($DENY_CNT,期望≥12)"

log ""; log "═══════════════════════════════════════════════"
log "  B2+B2.1+B2.2 验证: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
if grep -rEo 'Bearer [A-Za-z0-9_-]{20,}' "$OUT" 2>/dev/null | head -1 | grep -q .; then
  echo "  !!! 输出含 Bearer 明文 !!!" >> "$OUT"
fi
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
# B2.2:FAIL>0 必须非零退出(CI/证据收集不可接受)
[ "$FAIL" -eq 0 ] || exit 1
