#!/usr/bin/env bash
# MergePilot secret scanner — 2026-09-26 事故收口引入
# 门禁语义：任何真实凭据命中 → 退出码 1 → 禁止 commit（pre-commit）/ push（pre-push）/ release（CI）。
# 模式内置已知合成 fixture 豁免（ghp_abcdef*、AKIA*EXAMPLE 等文档示例值与占位符）。
# 用法：
#   scripts/secret-scan.sh --staged          # 扫暂存区（pre-commit）
#   scripts/secret-scan.sh --head [REF]      # 扫提交树（pre-push/CI，默认 HEAD）
#   scripts/secret-scan.sh --path DIR        # 扫干净 checkout 的目录（CI/发布前）
set -u

MODE="${1:--head}"; TARGET="${2:-HEAD}"
GH_TOKEN_CLASS='(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,40}'
PAT_FINE='github_pat_[A-Za-z0-9_]{60,}'
AWS='AKIA[0-9A-Z]{16}'
PEM='-----BEGIN [A-Z ]*PRIVATE KEY-----'
DSN='(postgres(ql)?|mysql(2)?|redis|amqp|mongodb(\+srv)?)://[A-Za-z0-9._%-]+:[^@/[:space:]]{4,}@'
ASSIGN='(password|passwd|secret|token|apikey|api_key)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}["'"'"']'
INCIDENT='promote-staging-2026'
PATTERNS="$GH_TOKEN_CLASS|$PAT_FINE|$AWS|$PEM|$DSN|$ASSIGN|$INCIDENT"

# 豁免：文档示例 / 占位符 / 测试值 / 环境变量引用 / 已证合成 fixture
EXCL='EXAMPLE|placeholder|changeme|dummy|sample|fake|your[-_]|xxx+|<[^>]+>|\$\{[^}]*\}|process\.env|os\.environ|secret-scan|wrong-password|test-password|abcdef|0{8,}|1{8,}|REDACTED|SUPERSECRET|INCIDENT='

RAW=$(mktemp); FLT=$(mktemp); trap 'rm -f "$RAW" "$FLT"' EXIT

case "$MODE" in
  --staged)
    TMPD=$(mktemp -d)
    git diff --cached --name-only -z | while IFS= read -r -d '' f; do
      mkdir -p "$TMPD/$(dirname "$f")" 2>/dev/null || true
      git show ":$f" > "$TMPD/$f" 2>/dev/null || true
    done
    ( cd "$TMPD" 2>/dev/null && grep -rInE "$PATTERNS" . 2>/dev/null ) > "$RAW"
    rm -rf "$TMPD" ;;
  --head)
    git grep -InE "$PATTERNS" "$TARGET" -- 2>/dev/null | sed "s|^\($TARGET:\)|\1|" > "$RAW" ;;
  --path)
    ( cd "$TARGET" 2>/dev/null && grep -rInE --exclude-dir=.git "$PATTERNS" . 2>/dev/null ) > "$RAW" ;;
  *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

grep -viE "$EXCL" "$RAW" > "$FLT" || true
N=$(grep -c . "$FLT" || true)
if [ "${N:-0}" -gt 0 ]; then
  echo "secret-scan: $N 处疑似真实凭据命中（已豁免文档示例/占位符/已证合成值）——禁止 commit/push/release："
  sed -E 's/((ghp|gho|ghu|ghs|ghr|github_pat_|AKIA)[A-Za-z0-9_]{6})[A-Za-z0-9_]*/\1…/g; s/([:=][[:space:]]*["'"'"'][^"'"'"']{4})[^"'"'"']*/\1…/g' "$FLT" | head -30
  exit 1
fi
echo "secret-scan: PASS（0 命中）"
exit 0
