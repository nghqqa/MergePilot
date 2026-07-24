#!/usr/bin/env bash
# gh-mcp-fix.sh — fixer 用:经 github MCP 一次性建修复分支 + 写修复文件 + 提修复 PR。
# 用法: gh-mcp-fix.sh <owner> <repo> <base_branch> <fix_branch> <file_path> <content_file> "<commit_msg>" "<pr_title>" <pr_body_file>
# content_file = 修复后的【完整文件内容】;pr_body_file = PR 说明(markdown)。
# PAT 在隔离 sidecar,本脚本不持有任何凭证。L2 高危不得调用(只出方案)。
set -uo pipefail
OWNER=${1:?owner}; REPO=${2:?repo}; BASE=${3:?base}; FIXB=${4:?fix_branch}
PATH_=${5:?path}; CONTENT_FILE=${6:?content_file}; MSG=${7:?commit_msg}
PRTITLE=${8:?pr_title}; PRBODY_FILE=${9:?pr_body_file}

CONTENT=$(cat "$CONTENT_FILE")
PRBODY=$(cat "$PRBODY_FILE")

echo "[gh-mcp-fix] 1) create_branch $FIXB from $BASE"
mcporter call github.create_branch owner=$OWNER repo=$REPO branch=$FIXB from_branch=$BASE 2>&1 | grep -iE "ref|already|409|422" | head -2 || true

echo "[gh-mcp-fix] 2) 取 $PATH_ @ $BASE 的 SHA"
SHA=$(mcporter call github.get_file_contents owner=$OWNER repo=$REPO path=$PATH_ ref=$BASE 2>/dev/null | grep -oE "SHA: [a-f0-9]+" | head -1 | awk "{print \$2}")
echo "SHA=${SHA:0:12}..."
[ -n "$SHA" ] || { echo "[gh-mcp-fix] ❌ 没取到 SHA,中止"; exit 1; }

echo "[gh-mcp-fix] 3) create_or_update_file($PATH_ on $FIXB)"
mcporter call github.create_or_update_file owner=$OWNER repo=$REPO path=$PATH_ \
  branch=$FIXB message="$MSG" sha=$SHA content="$CONTENT" 2>&1 | grep -iE "sha|html_url|commit" | head -3 || true

echo "[gh-mcp-fix] 4) create_pull_request $FIXB → $BASE"
mcporter call github.create_pull_request owner=$OWNER repo=$REPO \
  title="$PRTITLE" head=$FIXB base=$BASE body="$PRBODY" 2>&1 | grep -iE "html_url|number|\"url\"" | head -3
echo "[gh-mcp-fix] 完成"
