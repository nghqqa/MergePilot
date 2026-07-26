#!/bin/bash
# m3b-cutover-rollback.sh — 撤销 B1 割接,恢复 worker 直连 bridge(应急用)。
# 1. github-mcp 重新挂回 hiclab-net + hiclaw-net
# 2. 3 个 worker 的 mcporter.json 恢复成直连 github-mcp(无 token)
# 用法: wsl -- bash /mnt/d/goai/tools/m3b-cutover-rollback.sh
set -uo pipefail
echo "=== 回滚:github-mcp 挂回 hiclab-net + hiclaw-net ==="
docker network connect hiclab-net github-mcp 2>/dev/null && echo "  +hiclab-net" || echo "  (hiclab-net 已在)"
docker network connect hiclaw-net github-mcp 2>/dev/null && echo "  +hiclaw-net" || echo "  (hiclaw-net 已在)"
echo ""
echo "=== 回滚:worker mcporter.json → 直连 github-mcp(无 token)==="
BODY='{"mcpServers":{"github":{"url":"http://github-mcp:8082/sse","transport":"sse"}}}'
for W in reviewer fixer verifier; do
  docker exec "hiclaw-worker-$W" sh -c "mkdir -p /root/hiclaw-fs/agents/$W/config && cat > /root/hiclaw-fs/agents/$W/config/mcporter.json" <<JSON
$BODY
JSON
  echo "  $W → direct github-mcp:8082"
done
echo ""
echo "=== 验证:reviewer 直连 get_me ==="
docker exec hiclaw-worker-reviewer mcporter call github.get_me 2>&1 | head -3
echo ""
echo "回滚完成。gateway(policy-gw)仍在运行但 worker 不再走它;如需停 gateway: docker stop policy-gw"
