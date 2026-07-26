#!/bin/bash
# m3b-cutover-isolation.sh — B1 破坏性割接(封闭直连 bridge 的旁路)。
# 一次性原子完成:
#   1. 3 个 worker 的 mcporter.json 改指向 gateway(<role>/sse + 各自 Bearer token)
#   2. github-mcp 从 hiclab-net + hiclaw-net 摘除,只留 mcp-backend-net
#   3. 重启 gateway 让上游经 mcp-backend-net 重连
#   4. 验证:worker 直连 github-mcp:8082 BLOCKED;worker 经 gateway 调 get_me OK
# 前置:run-policy-gateway.sh 已跑(gateway 在 hiclab-net + mcp-backend-net 上)
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-cutover-isolation.sh
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-cutover-isolation.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

TOKENS=/home/ngh/.config/mergepilot/role-tokens.json
[ -f "$TOKENS" ] || { echo "缺 $TOKENS"; exit 1; }

log "═══════════════════════════════════════════════"
log "  B1 网络割接:封闭直连 bridge 旁路"
log "═══════════════════════════════════════════════"

# ─── 1. 改写 3 个 worker 的 mcporter.json(各持自己角色 token)───
log ""
log "=== 1. 改写 worker mcporter.json → gateway + 角色 token ==="
for W in reviewer fixer verifier; do
  TOK=$(python3 -c "import json;print(json.load(open('$TOKENS'))['$W'])")
  # token 走 env 不走 argv(避免 ps/日志泄露)
  docker exec -e TOK="$TOK" "hiclaw-worker-$W" sh -c '
    mkdir -p /root/hiclaw-fs/agents/'"$W"'/config
    python3 -c "
import json,os
cfg={\"mcpServers\":{\"github\":{\"baseUrl\":\"http://policy-gw:8083/'"$W"'/sse\",\"headers\":{\"Authorization\":\"Bearer \"+os.environ[\"TOK\"]}}}}
open(\"/root/hiclaw-fs/agents/'"$W"'/config/mcporter.json\",\"w\").write(json.dumps(cfg))
print(\"  $W mcporter.json → gateway/$W/sse (+bearer)\")
"
  ' >> "$OUT" 2>&1
done
# controller 持 coordinator token(放 controller.env,B4 用);本步先不接 controller 调用
log "  (controller 的 coordinator token 已在 role-tokens.json,B4 接入)"

# ─── 1b. 确保 github-mcp 在 mcp-backend-net(摘 worker 网的前置条件,否则 bridge 零网络)───
log ""
log "=== 1b. github-mcp 挂 mcp-backend-net(割接前置)==="
docker network connect mcp-backend-net github-mcp 2>/dev/null && log "  connected" || log "  (already on)"

# ─── 2. github-mcp 从两个 worker 网摘除 ───
log ""
log "=== 2. github-mcp 离开 hiclab-net + hiclaw-net(只留 mcp-backend-net)==="
docker network disconnect hiclab-net github-mcp 2>&1 >> "$OUT" && log "  disconnected hiclab-net" || log "  (hiclab-net already off)"
docker network disconnect hiclaw-net github-mcp 2>&1 >> "$OUT" && log "  disconnected hiclaw-net" || log "  (hiclaw-net already off)"
NETS=$(docker inspect github-mcp --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)
log "  github-mcp 现在的网络: ${NETS:-<none>}"

# ─── 3. 重启 gateway(让上游经 mcp-backend-net 重连)───
log ""
log "=== 3. 重启 policy-gw ==="
docker restart policy-gw >> "$OUT" 2>&1
sleep 6
docker logs --tail 3 policy-gw 2>&1 | grep -aiE "upstream ready|tools" >> "$OUT" || docker logs --tail 5 policy-gw 2>&1 >> "$OUT"

# ─── 4. 验证:旁路封闭 + gateway 通路 ───
log ""
log "=== 4a. reviewer 直连 github-mcp:8082 → 期望 BLOCKED ==="
BP=$(docker exec hiclaw-worker-reviewer python3 -c "
import socket;s=socket.socket();s.settimeout(2)
try:
 s.connect(('github-mcp',8082));print('BYPASS_OPEN')
except Exception as e:print('BLOCKED:',str(e)[:60])
" 2>/dev/null)
log "  $BP"
echo "$BP" | grep -q BLOCKED && ok "reviewer 无法直连 github-mcp(旁路封闭)" || bad "旁路仍开: $BP"

log ""
log "=== 4b. reviewer 经生产 mcporter.json 调 get_me → 期望 OK ==="
GM=$(docker exec hiclaw-worker-reviewer mcporter call github.get_me 2>&1 | head -8)
echo "$GM" | head -4 >> "$OUT"
echo "$GM" | grep -qiE "login|nghqqa" && ok "reviewer 经 gateway 调用成功(login 返回)" || bad "gateway 调用失败: $(echo "$GM"|head -3)"

log ""
log "=== 4c. fixer + verifier 各自 token 也通(身份隔离各自独立)==="
for W in fixer verifier; do
  GM2=$(docker exec "hiclaw-worker-$W" mcporter call github.get_me 2>&1 | head -3)
  echo "$GM2" | grep -qiE "login|nghqqa" && ok "$W 经 gateway 调用成功" || bad "$W gateway 调用失败: $(echo "$GM2"|head -2)"
done

log ""
log "=== 4d. github-mcp 仅在 mcp-backend-net(与 worker 不共网)==="
echo "$NETS" | grep -q "hiclab-net" && bad "github-mcp 仍在 hiclab-net" || ok "github-mcp 不在 hiclab-net"
echo "$NETS" | grep -q "hiclaw-net" && bad "github-mcp 仍在 hiclaw-net" || ok "github-mcp 不在 hiclaw-net"
echo "$NETS" | grep -q "mcp-backend-net" && ok "github-mcp 在 mcp-backend-net" || bad "github-mcp 不在 mcp-backend-net"

log ""
log "═══════════════════════════════════════════════"
log "  B1 网络割接验证: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
