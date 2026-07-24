#!/bin/bash
# 用新 PAT 更新 env 第 53 行 + 重启桥容器。新 PAT 经 NEWPAT 环境变量传入(不写进脚本)。
# 用法: wsl -- bash -c 'NEWPAT="ghp_xxx" bash /mnt/d/goai/tools/rewire-github-mcp-pat.sh'
set -euo pipefail
ENV=/home/ngh/hiclaw-manager.env
: "${NEWPAT:?NEWPAT env required}"

sed -i '53s|.*|HICLAW_GITHUB_TOKEN='"${NEWPAT}"'|' "$ENV"
echo "env line 53 已更新(脱敏):"
sed -n '53p' "$ENV" | sed 's/=.*/=***/'

echo ""
echo "=== 重启桥容器(读新 PAT)==="
bash /mnt/d/goai/tools/run-github-mcp-bridge.sh
