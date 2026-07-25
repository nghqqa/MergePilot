#!/bin/bash
# 干净重置:删除 reviewer/fixer/verifier(清旧房间+session)→ 重新创建(openclaw + deepseek-v4-flash)。
set -uo pipefail
echo "=== 1. 删除 3 个 worker ==="
for W in reviewer fixer verifier; do
  echo "--- delete $W ---"
  docker exec hiclaw-controller hiclaw delete worker "$W" 2>&1 | tail -1
done
sleep 5
echo ""
echo "=== 2. 重新创建 ==="
for W in reviewer fixer verifier; do
  echo "--- create $W ---"
  docker exec hiclaw-controller hiclaw create worker --name "$W" --model deepseek-v4-flash --runtime openclaw 2>&1 | tail -1
done
sleep 8
echo ""
echo "=== 3. 状态(应全 Running)==="
docker exec hiclaw-controller hiclaw get workers 2>&1
echo ""
echo "=== 4. 容器状态 ==="
docker ps --filter name=hiclaw-worker --format "{{.Names}} | {{.Status}}"
