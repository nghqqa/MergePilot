#!/bin/bash
# 宿主 runner:提取 admin 密码,运行 observe-demo.py。
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
LIMIT=${1:-10}
docker cp /mnt/d/goai/tools/observe-demo.py hiclaw-manager:/tmp/observe-demo.py
docker exec hiclaw-manager python3 /tmp/observe-demo.py "$PW" "$LIMIT"
