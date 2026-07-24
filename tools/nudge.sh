#!/bin/bash
# 通用 nudge:给 Manager 发一条消息。用法: bash nudge.sh "<message>"
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
MSG=${1:?message required}
docker cp /mnt/d/goai/tools/send-to-manager.py hiclaw-manager:/tmp/send-to-manager.py
docker exec hiclaw-manager python3 /tmp/send-to-manager.py "$PW" "$MSG"
