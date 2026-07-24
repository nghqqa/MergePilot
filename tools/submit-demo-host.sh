#!/bin/bash
# 宿主 runner:提取 admin 密码,把 submit_demo_pr1.py 传进 manager 并发送任务。
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
echo "admin password 长度=${#PW}"
[ -n "$PW" ] || { echo "❌ 密码为空"; exit 1; }
docker cp /mnt/d/goai/tools/submit_demo_pr1.py hiclaw-manager:/tmp/submit_demo_pr1.py
docker exec hiclaw-manager python3 /tmp/submit_demo_pr1.py "$PW"
