#!/bin/bash
# m3b-b2-test.sh — B2 最小权限矩阵验证(deny-by-default + fixer arg 校验 + L2 占位)。
# 用 probe-tools.py(mcp client 直连 gateway,token 从 gateway env 读)做精确判定,
# 避开 mcporter list --json 的控制字符解析问题。全部测 DENY 路径或只读 ALLOW,不触发真实写。
#   A. reviewer 可见工具不含 merge/create_branch(layer-1 过滤)
#   B. reviewer get_me ALLOW(读通)
#   C. fixer 可见工具含 create_branch,不含 merge
#   D. fixer create_branch 分支非 fix/ → DENY BRANCH_NOT_FIX_PREFIX
#   E. fixer 写 main → DENY BRANCH_PROTECTED
#   F. fixer 写 .env → DENY PATH_DENIED
#   G. fixer 写非 allowlist 仓库 → DENY REPO_NOT_ALLOWED
#   H. coordinator 可见工具含 merge
#   I. coordinator merge → DENY L2_TICKET_REQUIRED(B2 占位)
#   J. 审计有对应 DENY 行
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-b2-test.sh
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b2-test.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

PV=$(docker logs policy-gw 2>&1 | grep -aoE "policy_version=[a-z0-9-]+" | tail -1 | cut -d= -f2)
log "═══════════════════════════════════════════════"
log "  B2 最小权限矩阵验证 (policy=$PV)"
log "═══════════════════════════════════════════════"

# 装载探针
docker cp /mnt/d/goai/tools/policy-gateway/probe-tools.py policy-gw:/tmp/probe-tools.py 2>/dev/null

probe_list(){  # $1=role → 输出工具名 JSON 数组
  docker exec policy-gw python3 /tmp/probe-tools.py "$1" 2>/dev/null
}
probe_call(){  # $1=role $2=tool $3..=args → 输出结果文本
  docker exec policy-gw python3 /tmp/probe-tools.py "$1" --call "${@:2}" 2>/dev/null
}
has_tool(){ echo "$1" | python3 -c "import sys,json;print('$2' in json.load(sys.stdin))"; }

# ─── A. reviewer list 过滤 ───
log ""
log "=== A. reviewer 可见工具不含 merge_pull_request / create_branch ==="
REV=$(probe_list reviewer)
REV_N=$(echo "$REV" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
HM=$(has_tool "$REV" merge_pull_request)
HC=$(has_tool "$REV" create_branch)
HR=$(has_tool "$REV" get_me)
log "  reviewer 可见 $REV_N 工具: merge=$HM create_branch=$HC get_me=$HR"
[ "$HM" = "False" ] && [ "$HC" = "False" ] && [ "$HR" = "True" ] && ok "reviewer list 过滤正确(无 merge/create_branch,有 get_me,$REV_N 工具)" || bad "reviewer list 异常: merge=$HM create=$HC get_me=$HR"

# ─── B. reviewer get_me ALLOW ───
log ""
log "=== B. reviewer get_me → ALLOW ==="
GM=$(probe_call reviewer get_me owner=nghqqa repo=MergePilot)
echo "$GM" | head -2 >> "$OUT"
echo "$GM" | grep -qiE "login|nghqqa" && ok "reviewer 读调用成功" || bad "reviewer 读失败: $(echo "$GM"|head -2)"

# ─── C. fixer list 含 create_branch 不含 merge ───
log ""
log "=== C. fixer 可见工具含 create_branch,不含 merge ==="
FIX=$(probe_list fixer)
FIX_N=$(echo "$FIX" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
FHM=$(has_tool "$FIX" merge_pull_request)
FHC=$(has_tool "$FIX" create_branch)
log "  fixer 可见 $FIX_N 工具: merge=$FHM create_branch=$FHC"
[ "$FHM" = "False" ] && [ "$FHC" = "True" ] && ok "fixer list 正确(有 create_branch,无 merge,$FIX_N 工具)" || bad "fixer list 异常: merge=$FHM create=$FHC"

# ─── D-G. fixer 写 arg 校验(全 DENY)───
log ""
log "=== D. fixer create_branch 分支=evil/x(非 fix/)→ DENY BRANCH_NOT_FIX_PREFIX ==="
D=$(probe_call fixer create_branch owner=nghqqa repo=MergePilot branch=evil/x from=main)
echo "$D" | tail -2 >> "$OUT"
echo "$D" | grep -qiE "POLICY_DENIED.*BRANCH_NOT_FIX_PREFIX" && ok "非 fix/ 分支被拒" || bad "非 fix/ 分支应拒: $(echo "$D"|tail -1)"

log ""
log "=== E. fixer 写 main → DENY BRANCH_PROTECTED ==="
E=$(probe_call fixer create_or_update_file owner=nghqqa repo=MergePilot path=README.md branch=main content=x message=x)
echo "$E" | tail -2 >> "$OUT"
echo "$E" | grep -qiE "POLICY_DENIED.*BRANCH_PROTECTED" && ok "写 main 被拒(受保护)" || bad "写 main 应拒: $(echo "$E"|tail -1)"

log ""
log "=== F. fixer 写 .env → DENY PATH_DENIED ==="
F=$(probe_call fixer create_or_update_file owner=nghqqa repo=MergePilot path=.env branch=fix/test content=x message=x)
echo "$F" | tail -2 >> "$OUT"
echo "$F" | grep -qiE "POLICY_DENIED.*PATH_DENIED" && ok "写 .env 被拒(路径 denylist)" || bad "写 .env 应拒: $(echo "$F"|tail -1)"

log ""
log "=== G. fixer 写非 allowlist 仓库 → DENY REPO_NOT_ALLOWED ==="
G=$(probe_call fixer create_or_update_file owner=evil repo=other path=x.txt branch=fix/test content=x message=x)
echo "$G" | tail -2 >> "$OUT"
echo "$G" | grep -qiE "POLICY_DENIED.*REPO_NOT_ALLOWED" && ok "非 allowlist 仓库被拒" || bad "非 allowlist 仓库应拒: $(echo "$G"|tail -1)"

# ─── H/I. coordinator ───
log ""
log "=== H. coordinator 可见工具含 merge_pull_request ==="
COORD=$(probe_list coordinator)
COORD_N=$(echo "$COORD" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
CHM=$(has_tool "$COORD" merge_pull_request)
log "  coordinator 可见 $COORD_N 工具: merge=$CHM"
[ "$CHM" = "True" ] && ok "coordinator 能看到 merge($COORD_N 工具)" || bad "coordinator 应见 merge: merge=$CHM"

log ""
log "=== I. coordinator merge → DENY L2_TICKET_REQUIRED(B2 占位)==="
I=$(probe_call coordinator merge_pull_request owner=nghqqa repo=MergePilot pullNumber=999)
echo "$I" | tail -2 >> "$OUT"
echo "$I" | grep -qiE "POLICY_DENIED.*L2_TICKET_REQUIRED" && ok "coordinator merge 被拒(L2 需票据,B2 占位)" || bad "merge 应 L2 拒: $(echo "$I"|tail -1)"

# ─── J. 审计 ───
log ""
log "=== J. 审计 DENY 行(本轮)==="
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT ts,caller_agent,tool,decision,reason_code FROM mcp_calls WHERE decision='DENY' ORDER BY ts DESC LIMIT 10;" 2>/dev/null >> "$OUT"
DENY_CNT=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE reason_code IN ('BRANCH_NOT_FIX_PREFIX','BRANCH_PROTECTED','PATH_DENIED','REPO_NOT_ALLOWED','L2_TICKET_REQUIRED','TOOL_NOT_ALLOWED');" 2>/dev/null)
log "  策略 DENY 总数: ${DENY_CNT}"
[ "${DENY_CNT:-0}" -ge 5 ] && ok "审计记录了 $DENY_CNT 条策略 DENY" || bad "策略 DENY 审计不足($DENY_CNT)"

log ""
log "═══════════════════════════════════════════════"
log "  B2 验证: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"

# 安全扫描:确认输出无任何 Bearer 明文(B1 教训)
if grep -rEo 'Bearer [A-Za-z0-9_-]{20,}' "$OUT" 2>/dev/null | head -1 | grep -q .; then
  echo "  !!! 警告:输出含 Bearer 明文,不要进 git !!!" >> "$OUT"
fi
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
