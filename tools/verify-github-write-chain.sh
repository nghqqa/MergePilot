#!/bin/bash
# 验证 GitHub MCP 写链路(在 fixer 容器内跑):建修复分支 → 更新漏洞文件为修复版 → 提修复 PR。
set -uo pipefail
OWNER=nghqqa
REPO=mergepilot-test
SRC=feature/vulnerable-pr
FIX=fix/security-hardening
BASE=main

echo "=== 1. create_branch: $FIX (from $SRC) ==="
mcporter call github.create_branch owner=$OWNER repo=$REPO branch=$FIX from_branch=$SRC 2>&1 | head -10
echo ""

echo "=== 2. 取 user_service.py 在 $SRC 的 SHA ==="
SHA=$(mcporter call github.get_file_contents owner=$OWNER repo=$REPO path=user_service.py ref=$SRC 2>/dev/null | grep -oE "SHA: [a-f0-9]+" | head -1 | awk "{print \$2}")
echo "SHA=$SHA"
echo ""

echo "=== 3. create_or_update_file: 写入修复版 user_service.py ==="
FIXED='import sqlite3
import os

# 修复:API_KEY 从环境变量读取,不再硬编码(原 sk-live 硬编码密钥)
API_KEY = os.environ.get("API_KEY", "")


def get_user(name):
    conn = sqlite3.connect("db.sqlite")
    # 修复:参数化查询,消除 SQL 注入(原字符串拼接)
    return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchall()
'
mcporter call github.create_or_update_file owner=$OWNER repo=$REPO path=user_service.py \
  branch=$FIX message="fix(security): 参数化查询修复 SQLi;API_KEY 改读环境变量" sha=$SHA content="$FIXED" 2>&1 | head -12
echo ""

echo "=== 4. create_pull_request: $FIX -> $BASE ==="
mcporter call github.create_pull_request owner=$OWNER repo=$REPO \
  title="[MergePilot] 安全修复: SQLi + 硬编码密钥" head=$FIX base=$BASE \
  body="MergePilot Fixer 经 GitHub MCP 自动修复: 1) get_user 改参数化查询(修复 SQL 注入); 2) API_KEY 改为环境变量读取(修复硬编码密钥)." 2>&1 | head -15
