#!/bin/bash
# 直接把 github MCP 条目写进各 worker 容器的 mcporter 配置路径(mcporter 读 /root/hiclaw-fs/agents/<W>/config/mcporter.json)。
# 每个容器内独立执行(共享 FS 路径在每个 worker 本地)。
set -uo pipefail
BODY='{"mcpServers":{"github":{"url":"http://github-mcp:8082/sse","transport":"sse"}}}'

for W in fixer reviewer verifier; do
  echo "=== $W ==="
  # 先确保 hiclaw-fs 同步过(建好 agents/<W> 目录),再写 config
  docker exec "hiclaw-worker-$W" hiclaw-sync >/dev/null 2>&1 || true
  docker exec "hiclaw-worker-$W" sh -c "mkdir -p /root/hiclaw-fs/agents/$W/config && cat > /root/hiclaw-fs/agents/$W/config/mcporter.json <<'JSON'
$BODY
JSON"
  docker exec "hiclaw-worker-$W" mcporter list 2>&1 | grep -E "github|server|healthy" | head -4
done
