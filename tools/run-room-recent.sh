#!/bin/bash
# 宿主 runner:查指定 agent 房间最近 N 条消息。用法: bash run-room-recent.sh <agent> [n]
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
AGENT=${1:?agent}; N=${2:-6}
docker cp /mnt/d/goai/tools/room-recent.py hiclaw-manager:/tmp/room-recent.py
docker exec hiclaw-manager python3 /tmp/room-recent.py "$PW" "$AGENT" "$N"
