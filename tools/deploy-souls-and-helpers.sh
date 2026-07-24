#!/bin/bash
# 部署更新后的 SOUL + github MCP 封装脚本到 MinIO + worker。宿主侧运行。
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
MUSER=$(awk 'NR==45' "$ENV"); MUSER=${MUSER#*=}; MUSER=${MUSER%$'\r'}
MPASS=$(awk 'NR==46' "$ENV"); MPASS=${MPASS#*=}; MPASS=${MPASS%$'\r'}

# 拷源文件进 controller 临时目录
docker cp /mnt/d/goai/workers/reviewer/SOUL.md hiclaw-controller:/tmp/reviewer-SOUL.md
docker cp /mnt/d/goai/workers/fixer/SOUL.md    hiclaw-controller:/tmp/fixer-SOUL.md
docker cp /mnt/d/goai/workers/skills/gh-mcp/gh-mcp-read.sh hiclaw-controller:/tmp/gh-mcp-read.sh
docker cp /mnt/d/goai/workers/skills/gh-mcp/gh-mcp-fix.sh  hiclaw-controller:/tmp/gh-mcp-fix.sh

# 1. mc 写进 MinIO(worker 重启后 sync 会拉取)
docker exec hiclaw-controller bash -c "
mc alias set local http://localhost:9000 '$MUSER' '$MPASS' >/dev/null 2>&1
mc cp /tmp/reviewer-SOUL.md local/hiclaw-storage/agents/reviewer/SOUL.md
mc cp /tmp/fixer-SOUL.md    local/hiclaw-storage/agents/fixer/SOUL.md
mc cp /tmp/gh-mcp-read.sh   local/hiclaw-storage/agents/reviewer/skills/gh-mcp/gh-mcp-read.sh
mc cp /tmp/gh-mcp-fix.sh    local/hiclaw-storage/agents/fixer/skills/gh-mcp/gh-mcp-fix.sh
echo mc-deploy-done
"

# 2. helpers 直接放进 worker 的 /usr/local/bin(本会话立即可用,且在 PATH)
for W in reviewer fixer; do
  docker start "hiclaw-worker-$W" >/dev/null 2>&1 || true
  docker cp /mnt/d/goai/workers/skills/gh-mcp/gh-mcp-read.sh "hiclaw-worker-$W:/usr/local/bin/gh-mcp-read.sh" 2>/dev/null || true
  docker cp /mnt/d/goai/workers/skills/gh-mcp/gh-mcp-fix.sh  "hiclaw-worker-$W:/usr/local/bin/gh-mcp-fix.sh" 2>/dev/null || true
  docker exec "hiclaw-worker-$W" chmod +x /usr/local/bin/gh-mcp-read.sh /usr/local/bin/gh-mcp-fix.sh 2>/dev/null || true
done

# 3. 重启 reviewer + fixer(触发 sync 拉取新 SOUL + helpers)
docker restart hiclaw-worker-reviewer hiclaw-worker-fixer >/dev/null 2>&1
echo "部署完成。reviewer/fixer 重启中,会 sync 新 SOUL + github MCP 封装脚本。"
