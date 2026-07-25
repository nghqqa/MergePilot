#!/bin/bash
# 部署 handoff_watcher.py 到 manager 容器并后台常驻(无缓冲,密码经 env 传入不进 argv)。
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
docker exec hiclaw-manager sh -c "pkill -f handoff_watcher" 2>/dev/null || true
sleep 1
docker cp /mnt/d/goai/tools/handoff_watcher.py hiclaw-manager:/tmp/handoff_watcher.py
docker exec -d -e ADMIN_PW="$PW" hiclaw-manager bash -c "python3 -u /tmp/handoff_watcher.py > /tmp/watcher.log 2>&1"
sleep 5
echo "=== watcher 日志 ==="
docker exec hiclaw-manager cat /tmp/watcher.log 2>&1 | head -12
echo "--- 进程(ps 里不应见密码,因走 env)---"
docker exec hiclaw-manager sh -c "ps aux 2>/dev/null | grep handoff_watcher | grep -v grep" 2>&1 | sed -E 's/[a-zA-Z0-9]{20,}/***/g' | head -2
