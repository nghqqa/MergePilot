#!/bin/bash
# m3b-b2-test.sh — B2 + B2.1 hardening 验证。
# 用 probe-tools.py(mcp client 直连 gateway)做精确判定。全部 DENY 或无副作用读/搜索。
#
# B2 基线:A reviewer 过滤 / B reviewer 读 / C fixer 过滤 / D-G fixer 写约束 / H coord 过滤 / I coord L2 / J 审计
# B2.1 hardening:
#   D2  create_branch(from_branch=evil) → BASE_NOT_ALLOWED  (was: from_branch 绕过)
#   J2  coordinator create_or_update_file → TOOL_NOT_ALLOWED (was: coord 继承 fix 过权)
#   K   fixer assign_copilot_to_issue → TOOL_NOT_ALLOWED     (disabled)
#   L   reviewer 读非 allowlist repo → REPO_NOT_ALLOWED      (was: 读未覆盖 allowlist)
#   M   reviewer search_code 无 repo: → SEARCH_SCOPE_NOT_ALLOWED
#   N   reviewer search_code repo:allowlist → ALLOW
#   O   fixer update_pull_request(state=open) → L2_TICKET_REQUIRED
#   P   fixer update_pull_request(base=evil) → PR_FIELD_NOT_ALLOWED
#   Q   fixer update_pull_request(draft=true) → PR_FIELD_NOT_ALLOWED
#   R   fixer update_pull_request(title=...) → ALLOW(字段白名单通过)
#   S   create_repository/fork_repository 对所有角色不可见
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-b2-test.sh
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b2-test.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

PV=$(docker logs policy-gw 2>&1 | grep -aoE "policy_version=[a-z0-9.-]+" | tail -1 | cut -d= -f2)
log "═══════════════════════════════════════════════"
log "  B2 + B2.1 hardening 验证 (policy=$PV)"
log "═══════════════════════════════════════════════"

docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py 2>/dev/null
probe_list(){ docker exec policy-gw python3 /tmp/probe-tools.py "$1" 2>/dev/null; }
probe_call(){ docker exec policy-gw python3 /tmp/probe-tools.py "$1" --call "${@:2}" 2>/dev/null; }
has(){ echo "$1" | python3 -c "import sys,json;print('$2' in json.load(sys.stdin))"; }
deny_is(){ echo "$1" | grep -qiE "POLICY_DENIED.*$2"; }
allowed(){ echo "$1" | grep -qi POLICY_DENIED && echo False || echo True; }  # 无 POLICY_DENIED = 放行

DISABLED="create_repository fork_repository search_repositories search_users assign_copilot_to_issue"
disabled_visible(){ # $1=list-json → 输出可见的 disabled 工具数
  echo "$1" | python3 -c "
import sys,json
d=json.load(sys.stdin); dis='$DISABLED'.split()
print(sum(1 for t in dis if t in d))
"
}

# ─── A. reviewer 过滤(无写/L2/disabled)───
log ""; log "=== A. reviewer list 无 merge/create_branch/disabled 工具 ==="
REV=$(probe_list reviewer); RN=$(echo "$REV"|python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
DV=$(disabled_visible "$REV")
log "  reviewer($RN 工具): merge=$(has "$REV" merge_pull_request) create=$(has "$REV" create_branch) get_me=$(has "$REV" get_me) disabled可见=$DV"
[ "$(has "$REV" merge_pull_request)" = False ] && [ "$(has "$REV" create_branch)" = False ] && [ "$(has "$REV" get_me)" = True ] && ok "reviewer 无 merge/create_branch,有 get_me($RN)" || bad "reviewer 过滤异常"
[ "$DV" = 0 ] && ok "reviewer 无 disabled 工具可见" || bad "reviewer 可见 $DV 个 disabled 工具"

# ─── B. reviewer get_me ALLOW ───
log ""; log "=== B. reviewer get_me → ALLOW ==="
GM=$(probe_call reviewer get_me owner=nghqqa repo=MergePilot); echo "$GM"|head -1 >> "$OUT"
echo "$GM" | grep -qiE "login|nghqqa" && ok "reviewer 读成功" || bad "reviewer 读失败"

# ─── C. fixer 过滤(有 create_branch+update_pull_request,无 merge/disabled)───
log ""; log "=== C. fixer list 有 create_branch+update_pull_request,无 merge/disabled ==="
FIX=$(probe_list fixer); FN=$(echo "$FIX"|python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
FDV=$(disabled_visible "$FIX")
log "  fixer($FN 工具): create=$(has "$FIX" create_branch) upd=$(has "$FIX" update_pull_request) merge=$(has "$FIX" merge_pull_request) disabled可见=$FDV"
[ "$(has "$FIX" create_branch)" = True ] && [ "$(has "$FIX" update_pull_request)" = True ] && [ "$(has "$FIX" merge_pull_request)" = False ] && ok "fixer 有 create_branch+update_pull_request,无 merge($FN)" || bad "fixer 过滤异常"
[ "$FDV" = 0 ] && ok "fixer 无 disabled 工具可见" || bad "fixer 可见 $FDV 个 disabled 工具"

# ─── D. fixer create_branch from_branch=evil → BASE_NOT_ALLOWED(B2.1 绕过修复)───
log ""; log "=== D. fixer create_branch(from_branch=evil) → BASE_NOT_ALLOWED [B2.1] ==="
D=$(probe_call fixer create_branch owner=nghqqa repo=MergePilot branch=fix/x from_branch=evil)
echo "$D"|tail -1 >> "$OUT"
deny_is "$D" "BASE_NOT_ALLOWED" && ok "from_branch=evil 被拒(BASE_NOT_ALLOWED)" || bad "from_branch 绕过仍存在: $(echo "$D"|tail -1)"

# ─── E/F/G. fixer 写约束 ───
log ""; log "=== E. fixer 写 main → BRANCH_PROTECTED ==="
E=$(probe_call fixer create_or_update_file owner=nghqqa repo=MergePilot path=README.md branch=main content=x message=x)
echo "$E"|tail -1 >> "$OUT"; deny_is "$E" "BRANCH_PROTECTED" && ok "写 main 被拒" || bad "写 main 应拒"
log ""; log "=== F. fixer 写 .env → PATH_DENIED ==="
F=$(probe_call fixer create_or_update_file owner=nghqqa repo=MergePilot path=.env branch=fix/t content=x message=x)
echo "$F"|tail -1 >> "$OUT"; deny_is "$F" "PATH_DENIED" && ok "写 .env 被拒" || bad "写 .env 应拒"
log ""; log "=== G. fixer 写非 allowlist repo → REPO_NOT_ALLOWED ==="
G=$(probe_call fixer create_or_update_file owner=evil repo=other path=x.txt branch=fix/t content=x message=x)
echo "$G"|tail -1 >> "$OUT"; deny_is "$G" "REPO_NOT_ALLOWED" && ok "非 allowlist repo 被拒" || bad "非 allowlist repo 应拒"

# ─── H. coordinator 过滤(有 merge+update_pull_request,无 fix 类工具)───
log ""; log "=== H. coordinator list 有 merge+update_pull_request,无 create_or_update_file ==="
COORD=$(probe_list coordinator); CN=$(echo "$COORD"|python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
CDV=$(disabled_visible "$COORD")
log "  coordinator($CN 工具): merge=$(has "$COORD" merge_pull_request) upd=$(has "$COORD" update_pull_request) couf=$(has "$COORD" create_or_update_file) cbr=$(has "$COORD" create_branch) disabled可见=$CDV"
[ "$(has "$COORD" merge_pull_request)" = True ] && [ "$(has "$COORD" update_pull_request)" = True ] && [ "$(has "$COORD" create_or_update_file)" = False ] && [ "$(has "$COORD" create_branch)" = False ] && ok "coordinator 有 merge+upd,无 fix 类工具($CN)" || bad "coordinator 过权异常"

# ─── I. coordinator merge → L2 ───
log ""; log "=== I. coordinator merge → L2_TICKET_REQUIRED ==="
I=$(probe_call coordinator merge_pull_request owner=nghqqa repo=MergePilot pullNumber=999)
echo "$I"|tail -1 >> "$OUT"; deny_is "$I" "L2_TICKET_REQUIRED" && ok "coordinator merge 被拒(L2)" || bad "merge 应 L2 拒"

# ─── J2. coordinator create_or_update_file → TOOL_NOT_ALLOWED(B2.1 过权修复)───
log ""; log "=== J2. coordinator create_or_update_file → TOOL_NOT_ALLOWED [B2.1] ==="
J2=$(probe_call coordinator create_or_update_file owner=nghqqa repo=MergePilot path=x branch=fix/t content=x message=x)
echo "$J2"|tail -1 >> "$OUT"; deny_is "$J2" "TOOL_NOT_ALLOWED" && ok "coordinator 无 fix 类工具(过权修复)" || bad "coordinator 仍能调 create_or_update_file: $(echo "$J2"|tail -1)"

# ─── K. fixer assign_copilot_to_issue → TOOL_NOT_ALLOWED ───
log ""; log "=== K. fixer assign_copilot_to_issue → TOOL_NOT_ALLOWED [disabled] ==="
K=$(probe_call fixer assign_copilot_to_issue owner=nghqqa repo=MergePilot issueNumber=1)
echo "$K"|tail -1 >> "$OUT"; deny_is "$K" "TOOL_NOT_ALLOWED" && ok "assign_copilot_to_issue 被拒(disabled)" || bad "assign_copilot 不该可用"

# ─── L. reviewer 读非 allowlist repo → REPO_NOT_ALLOWED(B2.1 读覆盖)───
log ""; log "=== L. reviewer 读非 allowlist repo → REPO_NOT_ALLOWED [B2.1] ==="
L=$(probe_call reviewer get_file_contents owner=evil repo=other path=x path=x)
echo "$L"|tail -1 >> "$OUT"; deny_is "$L" "REPO_NOT_ALLOWED" && ok "reviewer 读非 allowlist repo 被拒" || bad "reviewer 读未覆盖 allowlist: $(echo "$L"|tail -1)"

# ─── M/N. search scope ───
log ""; log "=== M. reviewer search_code 无 repo: → SEARCH_SCOPE_NOT_ALLOWED [B2.1] ==="
M=$(probe_call reviewer search_code query="password")
echo "$M"|tail -1 >> "$OUT"; deny_is "$M" "SEARCH_SCOPE_NOT_ALLOWED" && ok "无 repo: 限定的搜索被拒" || bad "无 scope 搜索应拒"
log ""; log "=== N. reviewer search_code repo:allowlist → ALLOW ==="
N=$(probe_call reviewer search_code query="repo:nghqqa/MergePilot password")
echo "$N"|tail -1 >> "$OUT"; [ "$(allowed "$N")" = True ] && ok "repo:allowlist 限定的搜索放行" || bad "scoped 搜索应放行: $(echo "$N"|tail -1)"

# ─── O/P/Q/R. update_pull_request 字段白名单 ───
log ""; log "=== O. fixer update_pull_request(state=open) → L2_TICKET_REQUIRED ==="
O=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999 state=open)
echo "$O"|tail -1 >> "$OUT"; deny_is "$O" "L2_TICKET_REQUIRED" && ok "state=open 被拒(L2)" || bad "state=open 应 L2 拒"
log ""; log "=== P. fixer update_pull_request(base=evil) → PR_FIELD_NOT_ALLOWED ==="
P=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999 base=evil)
echo "$P"|tail -1 >> "$OUT"; deny_is "$P" "PR_FIELD_NOT_ALLOWED" && ok "base 字段被拒" || bad "base 应拒"
log ""; log "=== Q. fixer update_pull_request(draft=true) → PR_FIELD_NOT_ALLOWED ==="
Q=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999 draft=true)
echo "$Q"|tail -1 >> "$OUT"; deny_is "$Q" "PR_FIELD_NOT_ALLOWED" && ok "draft 字段被拒" || bad "draft 应拒"
log ""; log "=== R. fixer update_pull_request(title=...) → ALLOW(字段白名单通过)==="
R=$(probe_call fixer update_pull_request owner=nghqqa repo=MergePilot pullNumber=999999 title=test-title)
echo "$R"|tail -1 >> "$OUT"; [ "$(allowed "$R")" = True ] && ok "title/body 字段放行(GitHub 对不存在 PR 报错无副作用)" || bad "title 应放行: $(echo "$R"|tail -1)"

# ─── S. disabled 工具对所有角色不可见 ───
log ""; log "=== S. create_repository / fork_repository 对 coordinator 也不可见 ==="
[ "$(has "$COORD" create_repository)" = False ] && [ "$(has "$COORD" fork_repository)" = False ] && ok "create_repository/fork_repository 对 coordinator 不可见" || bad "disabled 工具对 coordinator 可见"

# ─── T. 审计 ───
log ""; log "=== T. 审计 DENY 行(本轮)==="
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT reason_code,count(*) FROM mcp_calls WHERE decision='DENY' GROUP BY reason_code ORDER BY count(*) DESC;" 2>/dev/null >> "$OUT"
DENY_CNT=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE reason_code IN ('BASE_NOT_ALLOWED','BRANCH_PROTECTED','PATH_DENIED','REPO_NOT_ALLOWED','L2_TICKET_REQUIRED','TOOL_NOT_ALLOWED','SEARCH_SCOPE_NOT_ALLOWED','PR_FIELD_NOT_ALLOWED');" 2>/dev/null)
log "  策略 DENY 总数: ${DENY_CNT}"
[ "${DENY_CNT:-0}" -ge 10 ] && ok "审计记录了 $DENY_CNT 条策略 DENY(≥10)" || bad "DENY 审计不足($DENY_CNT)"

log ""; log "═══════════════════════════════════════════════"
log "  B2+B2.1 验证: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
if grep -rEo 'Bearer [A-Za-z0-9_-]{20,}' "$OUT" 2>/dev/null | head -1 | grep -q .; then
  echo "  !!! 输出含 Bearer 明文 !!!" >> "$OUT"
fi
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
