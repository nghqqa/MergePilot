#!/bin/bash
# 启 v2 watcher(动态房间发现 + 任务房间内 @mention)。停掉 v1。
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
docker exec hiclaw-manager sh -c "pkill -f handoff_watcher" 2>/dev/null || true
sleep 1
docker cp /mnt/d/goai/tools/handoff_watcher_v2.py hiclaw-manager:/tmp/handoff_watcher_v2.py
docker exec -d -e ADMIN_PW="$PW" hiclaw-manager bash -c "python3 -u /tmp/handoff_watcher_v2.py > /tmp/watcher_v2.log 2>&1"
sleep 4
echo "=== v2 watcher 日志 ==="
docker exec hiclaw-manager cat /tmp/watcher_v2.log 2>&1 | head -6
