#!/usr/bin/env bash
# 回滚:verify FAIL 后,把 release-candidate 的 user_service.py 还原成干净版(revert commit)。
# 在 fixer 容器内跑。经 GitHub MCP 完成。
set -uo pipefail
OWNER=nghqqa; REPO=mergepilot-test; RC=release-candidate

echo "=== 取 release-candidate 当前(bad)SHA ==="
SHA=$(mcporter call github.get_file_contents owner=$OWNER repo=$REPO path=user_service.py ref=$RC 2>/dev/null | grep -oE "SHA: [a-f0-9]+" | head -1 | awk '{print $2}')
echo "bad SHA=${SHA:0:12}..."

echo "=== 还原为干净版(revert)==="
CLEAN=$(cat <<'PY'
import os
import sqlite3

API_KEY = os.environ.get("API_KEY", "")

def get_user(name: str) -> dict | None:
    """Return user by name."""
    if not name:
        return None
    with sqlite3.connect("db.sqlite") as conn:
        result = conn.execute(
            "SELECT * FROM users WHERE name = ?", (name,)
        ).fetchall()
    return {"user": result} if result else None
PY
)
mcporter call github.create_or_update_file owner=$OWNER repo=$REPO path=user_service.py \
  branch=$RC message="revert: rollback bad fix after verify FAIL (hardcoded key + SQLi reintroduced)" sha=$SHA content="$CLEAN" 2>&1 | grep -iE '"sha"|html_url' | head -2
echo "回滚完成"
