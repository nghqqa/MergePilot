#!/bin/bash
# m3b-probe.sh — M3-B 开建前的一次性环境探测。输出写到文件,避开 wsl stderr 噪声。
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-probe.sh
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-probe.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }

log "=== A. mcporter config add --help(看有无 --header / --auth) ==="
docker exec hiclaw-worker-reviewer mcporter config add --help 2>&1 | grep -aiE "header|auth|token|bearer|url|transport" >> "$OUT" || true
log ""
log "=== B. mcporter 二进制类型 + 位置 ==="
docker exec hiclaw-worker-reviewer sh -c 'which mcporter; file $(which mcporter) 2>/dev/null' >> "$OUT" 2>&1 || true
docker exec hiclaw-worker-reviewer sh -c 'pip show mcporter 2>/dev/null | head -6' >> "$OUT" 2>&1 || true
log ""
log "=== C. 在 mcporter 包源码里搜 header 支持 ==="
docker exec hiclaw-worker-reviewer sh -c '
MP=$(which mcporter);
# 若是 python 入口,顺藤摸包
PYPKG=$(python3 -c "import mcporter,os;print(os.path.dirname(mcporter.__file__))" 2>/dev/null);
if [ -n "$PYPKG" ]; then
  echo "py pkg: $PYPKG";
  grep -rniE "header|authorization|bearer" "$PYPKG" 2>/dev/null | grep -viE "test_|/tests/" | head -20;
fi
# 也扫 mcporter 同目录下有无 schema
ls -la $(dirname "$MP") 2>/dev/null | head -10
' >> "$OUT" 2>&1 || true
log ""
log "=== D. 各容器网络归属 ==="
for C in github-mcp hiclaw-worker-reviewer hiclaw-worker-fixer hiclaw-worker-verifier mergepilot-controller hiclaw-manager hiclaw-controller; do
  NETS=$(docker inspect "$C" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)
  log "  $C: ${NETS:-<none>}"
done
log ""
log "=== E. 两个网络的成员(反查) ==="
for N in hiclab-net hiclaw-net; do
  MEMBERS=$(docker network inspect "$N" --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null)
  log "  $N: ${MEMBERS:-<empty>}"
done
log ""
log "=== F. reviewer → github-mcp:8082 旁路确认 ==="
docker exec hiclaw-worker-reviewer python3 -c "import socket;s=socket.socket();s.settimeout(2);
import sys
try:
 s.connect(('github-mcp',8082));print('BYPASS_OPEN: reviewer can reach github-mcp:8082 directly')
except Exception as e:print('BYPASS_CLOSED:',e)" >> "$OUT" 2>&1 || true
log ""
log "=== G. mcporter 版本 ==="
docker exec hiclaw-worker-reviewer mcporter --version 2>&1 >> "$OUT" || true

echo "probe done -> $OUT"
