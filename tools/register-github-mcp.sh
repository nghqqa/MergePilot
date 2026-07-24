#!/bin/bash
# 在 manager 的 mcporter 配置里加 github 条目(指向凭证隔离桥),保留旧 mcp-github 条目,并测试 get_me。
set -uo pipefail
CFG=/root/manager-workspace/config/mcporter.json
cp "$CFG" "${CFG}.bak"

# 加 github 条目(sse 传输,无凭证 header — PAT 只在桥里)
tmp=$(mktemp)
jq '.mcpServers.github = {"url":"http://github-mcp:8082/sse","transport":"sse"}' "$CFG" > "$tmp" && mv "$tmp" "$CFG"
echo "=== 注册后 mcporter list ==="
mcporter list 2>&1 | head -15
echo ""
echo "=== 调 github.get_me(验证真实 GitHub API 端到端)==="
mcporter call github.get_me 2>&1 | head -25
