#!/usr/bin/env bash
# 在 release-candidate 集成分支上应用一个「坏修复」(干净代码 + 偷偷加回硬编码 token),
# 模拟 fixer 引入回归,作为回滚触发输入。在 fixer 容器内跑。
set -uo pipefail
OWNER=nghqqa; REPO=mergepilot-test; RC=release-candidate

echo "=== 1) 建 release-candidate (from main) ==="
mcporter call github.create_branch owner=$OWNER repo=$REPO branch=$RC from_branch=main 2>&1 | grep -iE '"ref"|already|422' | head -2 || true

echo "=== 2) 取 user_service.py 在 $RC 的 SHA ==="
SHA=$(mcporter call github.get_file_contents owner=$OWNER repo=$REPO path=user_service.py ref=$RC 2>/dev/null | grep -oE "SHA: [a-f0-9]+" | head -1 | awk '{print $2}')
echo "SHA=${SHA:0:12}..."

echo "=== 3) 应用坏修复(加回硬编码 INTERNAL_TOKEN)==="
BAD=$(cat <<'PY'
import sqlite3

API_KEY = "sk-live-abcdef0123456789"

def get_user(name):
    conn = sqlite3.connect("db.sqlite")
    return conn.execute("SELECT * FROM users WHERE name='" + name + "'").fetchall()
PY
)
mcporter call github.create_or_update_file owner=$OWNER repo=$REPO path=user_service.py \
  branch=$RC message="fix(input): add input hardening (bad attempt)" sha=$SHA content="$BAD" 2>&1 | grep -iE '"sha"|html_url' | head -2
echo "坏修复已应用到 $RC"
