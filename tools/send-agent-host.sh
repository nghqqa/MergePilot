#!/bin/bash
# 宿主 runner:给指定 agent 发消息。用法: bash send-agent-host.sh <agent> "<message>"
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
AGENT=${1:?agent}; MSG=${2:?message}
docker cp /mnt/d/goai/tools/send-to-agent.py hiclaw-manager:/tmp/send-to-agent.py
docker exec hiclaw-manager python3 /tmp/send-to-agent.py "$PW" "$AGENT" "$MSG"
