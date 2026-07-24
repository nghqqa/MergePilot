#!/usr/bin/env bash
# gh-mcp-read.sh — reviewer 用:经 github MCP 读仓库某分支的某文件,写到 /tmp/review/<文件名>。
# 用法: gh-mcp-read.sh <owner> <repo> <path> <ref>
# PAT 在隔离 sidecar,本脚本不持有任何凭证。
set -uo pipefail
OWNER=${1:?owner}; REPO=${2:?repo}; PATH_=${3:?path}; REF=${4:?ref}
mkdir -p /tmp/review
BASE=$(basename "$PATH_")
RAW=/tmp/review/.${BASE}.raw
OUT=/tmp/review/$BASE
echo "[gh-mcp-read] $OWNER/$REPO/$PATH_ @ $REF → $OUT"
mcporter call github.get_file_contents owner=$OWNER repo=$REPO path=$PATH_ ref=$REF > "$RAW" 2>&1 || { echo "[gh-mcp-read] mcporter 调用失败"; cat "$RAW"; exit 1; }
# 去掉 mcporter 的日志行("successfully downloaded ..." / "[mcporter] ..."),保留文件内容
grep -vE "^(successfully downloaded|\[mcporter\]|Error:|SseError)" "$RAW" > "$OUT" || cp "$RAW" "$OUT"
rm -f "$RAW"
echo "[gh-mcp-read] OK,内容行数=$(wc -l < "$OUT")"
echo "--- 预览(前 20 行)---"; head -20 "$OUT"
