#!/bin/bash
# demo_prepare.sh — Demo 前重启所有 Worker,保证干净状态(解决上下文粘滞问题)
# 用法:bash tools/demo_prepare.sh
echo "=== 重启所有 Worker(清空上下文)==="
docker restart hiclaw-worker-reviewer hiclaw-worker-fixer hiclaw-worker-verifier 2>&1
echo "=== 等 30s 重连 ==="
sleep 30
echo "=== 验证全部 Running ==="
docker exec hiclaw-controller hiclaw get workers 2>&1
echo "=== Ready for Demo ==="
echo "提示:现在提交 PR,第一次任务在干净 Worker 上 = 最可靠"
