#!/bin/bash
# m3b-b1-test.sh — B1 非破坏性验证(不动 worker 生产配置,不动 bridge 网络)。
# 在 reviewer 容器里用临时 mcporter 配置打 gateway,验证:
#   A. 无 token → 401 BAD_TOKEN
#   B. reviewer token 打 /coordinator/sse → 401 ROLE_PATH_MISMATCH
#   C. reviewer token 打 /reviewer/sse → mcporter list 拿到 44 工具
#   D. reviewer token 调一个只读工具(get_me)→ 成功
#   E. audit-pg.mcp_calls 有对应 ALLOW + DENY 审计行
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-b1-test.sh
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b1-test.out
: > "$OUT"
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

TOKENS=/home/ngh/.config/mergepilot/role-tokens.json
REV_TOKEN=$(python3 -c "import json;print(json.load(open('$TOKENS'))['reviewer'])")

log(){ echo "$*" >> "$OUT"; }
log "═══════════════════════════════════════════════"
log "  B1 非破坏性验证 (gateway 已起,bridge 未割接)"
log "═══════════════════════════════════════════════"

# ─── A/B: HTTP 级认证测试(reviewer 容器内 python)───
log ""
log "=== A. 无 token 打 /reviewer/sse → 期望 401 BAD_TOKEN ==="
CODE_A=$(docker exec hiclaw-worker-reviewer python3 -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://policy-gw:8083/reviewer/sse', timeout=4)
    print('OK')  # 不该到这
except urllib.error.HTTPError as e:
    import sys; data=e.read().decode()[:120]
    print(f'{e.code} {data}')
except Exception as e:
    print('ERR:'+str(e)[:80])
" 2>/dev/null)
log "  resp: $CODE_A"
echo "$CODE_A" | grep -q "401" && echo "$CODE_A" | grep -qi "BAD_TOKEN" && ok "无 token → 401 BAD_TOKEN" || bad "无 token 期望 401 BAD_TOKEN,实际: $CODE_A"

log ""
log "=== B. reviewer token 打 /coordinator/sse → 期望 401 ROLE_PATH_MISMATCH ==="
CODE_B=$(docker exec -e REV_TOKEN="$REV_TOKEN" hiclaw-worker-reviewer python3 -c "
import urllib.request, urllib.error, os
req=urllib.request.Request('http://policy-gw:8083/coordinator/sse', headers={'Authorization':'Bearer '+os.environ['REV_TOKEN']})
try:
    urllib.request.urlopen(req, timeout=4); print('OK')
except urllib.error.HTTPError as e:
    print(f'{e.code} {e.read().decode()[:120]}')
except Exception as e:
    print('ERR:'+str(e)[:80])
" 2>/dev/null)
log "  resp: $CODE_B"
echo "$CODE_B" | grep -q "401" && echo "$CODE_B" | grep -qi "ROLE_PATH_MISMATCH" && ok "跨角色 → 401 ROLE_PATH_MISMATCH" || bad "跨角色期望 401 ROLE_PATH_MISMATCH,实际: $CODE_B"

# ─── C/D: mcporter 集成(临时 config,不动生产)───
log ""
log "=== C. reviewer token 经 mcporter 打 gateway → 期望 list 到工具 ==="
# 先用 config add --header 写临时配置(确认 mcporter.json headers 字段格式)
docker exec hiclaw-worker-reviewer sh -c "
rm -f /tmp/mcp_b1.json
mcporter --config /tmp/mcp_b1.json config add github \
  --url 'http://policy-gw:8083/reviewer/sse' --transport sse \
  --header 'Authorization=Bearer $REV_TOKEN' >/dev/null 2>&1
echo '--- 生成的临时 mcporter.json(token 已脱敏)---'
cat /tmp/mcp_b1.json | sed -E 's/(Bearer )[A-Za-z0-9_.-]+/\1<REDACTED>/g'
" >> "$OUT" 2>&1
# 真正 list
LIST_OUT=$(docker exec hiclaw-worker-reviewer sh -c "mcporter --config /tmp/mcp_b1.json list github 2>&1" 2>/dev/null)
log "--- mcporter list 输出(前 12 行)---"
echo "$LIST_OUT" | head -12 >> "$OUT"
# 用已知 GitHub 工具名存在性判定(比解析 JSON 形状稳)
KNOWN="get_me list_issues create_branch merge_pull_request add_issue_comment"
FOUND=0
for t in $KNOWN; do
  echo "$LIST_OUT" | grep -qw "$t" && FOUND=$((FOUND+1))
done
log "  已知工具命中: $FOUND/5 ($KNOWN)"
[ "$FOUND" -ge 4 ] && ok "reviewer 经 gateway list 到完整工具集($FOUND/5 已知工具命中)" || bad "list 工具不足(命中 $FOUND/5)"

log ""
log "=== D. reviewer token 调只读工具(get_me)==="
CALL_OUT=$(docker exec hiclaw-worker-reviewer sh -c "mcporter --config /tmp/mcp_b1.json call github.get_me 2>&1" 2>/dev/null | head -20)
echo "$CALL_OUT" | head -8 >> "$OUT"
echo "$CALL_OUT" | grep -qiE "login|node_id|Login|user" && ok "get_me 调用成功(返回 login)" || bad "get_me 未返回预期: $(echo "$CALL_OUT" | head -3)"

# ─── E: 审计验证 ───
log ""
log "=== E. audit-pg.mcp_calls 审计行 ==="
PG_PASS=$(grep -E '^PG_PASS=' /home/ngh/.config/mergepilot/controller.env | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
DENY_CNT=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE decision='DENY' AND reason_code IN ('BAD_TOKEN','ROLE_PATH_MISMATCH');" 2>/dev/null)
ALLOW_CNT=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c \
  "SELECT count(*) FROM mcp_calls WHERE decision='ALLOW';" 2>/dev/null)
log "  DENY(BAD_TOKEN/MISMATCH)=$DENY_CNT  ALLOW=$ALLOW_CNT"
docker exec audit-pg psql -U mergepilot -d mergepilot_audit -c \
  "SELECT ts,caller_agent,tool,decision,reason_code FROM mcp_calls ORDER BY ts DESC LIMIT 8;" 2>/dev/null >> "$OUT"
[ -n "$DENY_CNT" ] && [ "$DENY_CNT" -ge 2 ] && ok "审计记录了 $DENY_CNT 条 DENY(BAD_TOKEN + MISMATCH)" || bad "DENY 审计不足($DENY_CNT)"
[ -n "$ALLOW_CNT" ] && [ "$ALLOW_CNT" -ge 1 ] && ok "审计记录了 $ALLOW_CNT 条 ALLOW" || bad "无 ALLOW 审计"

log ""
log "═══════════════════════════════════════════════"
log "  B1 非破坏性验证: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
# 防篡改触发器烟雾测试(只在表非空时挑真实行 UPDATE,空表触发器不会 fire)
log ""
log "=== F. mcp_calls 不可变烟雾测试(UPDATE 真实行应被触发器拒)==="
ROW_CNT=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c "SELECT count(*) FROM mcp_calls;" 2>/dev/null)
if [ "${ROW_CNT:-0}" -ge 1 ]; then
  IMMUT=$(docker exec audit-pg psql -U mergepilot -d mergepilot_audit -t -A -c \
    "UPDATE mcp_calls SET decision='DENY' WHERE request_id=(SELECT request_id FROM mcp_calls LIMIT 1);" 2>&1 | head -3)
  echo "$IMMUT" >> "$OUT"
  echo "$IMMUT" | grep -qiE "INSERT-only|immutable" && ok "mcp_calls UPDATE 被拒(不可变,${ROW_CNT} 行)" || bad "mcp_calls 可被 UPDATE: $IMMUT"
else
  log "  表空(E 未通过),F 跳过"
  bad "mcp_calls 空,无法测不可变"
fi

echo "" >> "$OUT"; echo "  B1-test PASS=$PASS FAIL=$FAIL" >> "$OUT"
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
