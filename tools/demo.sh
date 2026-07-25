#!/bin/bash
# MergePilot 一键 Demo:起桥 → 配置 worker → watcher → 建任务房间 + 提交 PR。
# watcher 自动驱动 review→fix→verify,全程零人工 nudge + 零跨-PR 污染(per-task room)。
#
# 用法(Windows/WSL):
#   MSYS_NO_PATHCONV=1 wsl -- bash /mnt/d/goai/tools/demo.sh <branch> <pr_number> [prefix]
# 例:
#   MSYS_NO_PATHCONV=1 wsl -- bash /mnt/d/goai/tools/demo.sh feature/m1-e2e 6 demo-pr6
#
# 前提:HiClaw 环境(Manager + 3 Worker)已安装运行;github-mcp 桥镜像已 build。
set -uo pipefail
BRANCH=${1:?用法: demo.sh <branch> <pr_number> [prefix]}
PR=${2:?pr_number}
PREFIX=${3:-demo-pr${PR}}

echo "═══════════════════════════════════════════════"
echo "  MergePilot 一键 Demo"
echo "  PR #$PR | 分支 $BRANCH | 前缀 $PREFIX"
echo "═══════════════════════════════════════════════"

echo ""
echo "[1/5] 起 GitHub MCP 桥(凭证隔离 sidecar,PAT 只进桥 env)..."
bash /mnt/d/goai/tools/run-github-mcp-bridge.sh 2>&1 | tail -3 | grep -v "^$"

echo ""
echo "[2/5] 部署 github MCP 配置到 3 个 worker..."
bash /mnt/d/goai/tools/deploy-github-mcp-direct.sh 2>&1 | grep -E "github|healthy" | head -3

echo ""
echo "[3/5] 部署 SOUL(MCP 指令)+ helper 脚本到 worker..."
bash /mnt/d/goai/tools/deploy-souls-and-helpers.sh 2>&1 | tail -1
echo "等 6s worker sync..."
sleep 6

echo ""
echo "[4/5] 起 v2 watcher(动态发现房间 + 真 @mention 驱动阶段交接)..."
bash /mnt/d/goai/tools/start-watcher-v2.sh 2>&1 | grep "w2" | head -1

echo ""
echo "[5/5] 建任务房间 + 发 @reviewer 真 mention 审查任务..."
bash /mnt/d/goai/tools/run-in-mgr.sh submit_pr_taskroom.py "task-${PREFIX}" "$PREFIX" "$BRANCH" "$PR" 2>&1 | grep -E "created|joined|posted"

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✅ Demo 已启动!"
echo "  watcher 会自动驱动 reviewer→fixer→verifier→裁定。"
echo ""
echo "  观察(从上面 created 行取 room_id):"
echo "    bash /mnt/d/goai/tools/run-room-recent.sh <room_id> 10"
echo ""
echo "  watcher 日志:"
echo "    docker exec hiclaw-manager tail -f /tmp/watcher_v2.log"
echo "═══════════════════════════════════════════════"
