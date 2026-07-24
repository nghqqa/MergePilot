#!/bin/bash
# 起凭证隔离桥容器,并 probe SSE 端点确认在服务。
# 用法: MSYS_NO_PATHCONV=1 wsl -- bash /mnt/d/goai/tools/run-github-mcp-bridge.sh
set -euo pipefail
ENV=/home/ngh/hiclaw-manager.env
TOKEN=$(awk 'NR==53' "$ENV"); TOKEN=${TOKEN#*=}; TOKEN=${TOKEN%$'\r'}
echo "token_len=${#TOKEN}"
[ "${#TOKEN}" -ge 40 ] || { echo "TOKEN 无效(<40)"; exit 1; }

docker rm -f github-mcp 2>/dev/null || true
docker run -d --name github-mcp --network hiclaw-net --restart unless-stopped \
  -e GITHUB_PERSONAL_ACCESS_TOKEN="$TOKEN" \
  github-mcp-bridge:latest

sleep 5
echo "=== 容器状态 + 最近日志 ==="
docker ps --filter name=github-mcp --format "{{.Names}} | {{.Status}}"
docker logs --tail 8 github-mcp 2>&1 || true
