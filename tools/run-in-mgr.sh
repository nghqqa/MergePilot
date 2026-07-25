#!/bin/bash
# 通用 host runner:提取 admin 密码(脚本文件,避开 inline awk 坑),把 tools/<script> 传进 manager 跑。
# 用法: bash run-in-mgr.sh <script.py> [args...]
set -uo pipefail
ENV=/home/ngh/hiclaw-manager.env
PW=$(awk 'NR==22' "$ENV"); PW=${PW#*=}; PW=${PW%$'\r'}
SCRIPT=${1:?script}; shift
docker cp "/mnt/d/goai/tools/$SCRIPT" hiclaw-manager:"/tmp/$SCRIPT"
docker exec hiclaw-manager python3 "/tmp/$SCRIPT" "$PW" "$@"
