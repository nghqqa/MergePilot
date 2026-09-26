#!/usr/bin/env bash
# 安装本地 pre-commit / pre-push secret 门禁（2026-09-26 事故收口引入）。
# 用法：bash scripts/install-hooks.sh   （在仓库根或任意 worktree 内执行）
set -eu
ROOT=$(git rev-parse --show-toplevel)
HOOKS=$(git rev-parse --git-path hooks)
mkdir -p "$HOOKS"

cat > "$HOOKS/pre-commit" <<'EOF'
#!/usr/bin/env bash
# pre-commit: 暂存区 secret 扫描，命中即拒绝提交
REPO=$(git rev-parse --show-toplevel)
if [ -f "$REPO/scripts/secret-scan.sh" ]; then
  bash "$REPO/scripts/secret-scan.sh" --staged || {
    echo "pre-commit: BLOCKED —— 疑似真实凭据，禁止提交。豁免需走 SECURITY.md 流程并登记理由。"
    exit 1
  }
fi
EOF

cat > "$HOOKS/pre-push" <<'EOF'
#!/usr/bin/env bash
# pre-push: 待推提交树 secret 扫描，命中即拒绝推送
REPO=$(git rev-parse --show-toplevel)
if [ -f "$REPO/scripts/secret-scan.sh" ]; then
  while read -r local_ref local_sha remote_ref remote_sha; do
    [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
    bash "$REPO/scripts/secret-scan.sh" --head "$local_sha" || {
      echo "pre-push: BLOCKED —— $local_sha 含疑似真实凭据，禁止推送。"
      exit 1
    }
  done
fi
EOF

chmod +x "$HOOKS/pre-commit" "$HOOKS/pre-push"
echo "installed: $HOOKS/pre-commit, $HOOKS/pre-push"
