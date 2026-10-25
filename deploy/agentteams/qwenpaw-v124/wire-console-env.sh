#!/usr/bin/env bash
# wire-console-env.sh — console 接线键幂等合并进 console 栈 env 文件。
# 声明化接线的 env 半边（网络半边=deploy/selfhost/docker-compose.agentteams.yml）：
#   wire-console-env.sh <WIRE_FILE> <CONSOLE_ENV_FILE> [KEY=VALUE ...]
#   * WIRE_FILE=安装入口出仓的 console-wire.env（MU_AGENTTEAMS_* 键）
#   * KEY=VALUE 直通合并（入口用其写 MU_AGENTTEAMS_NETWORK=<QP_NETWORK>——
#     该键供 compose 叠加文件插值，不进 wire 清单）
# 幂等契约：同键覆盖且仅一份、不重复追加、无关行不动、0600 保持；不回显任何值。
set -euo pipefail
WIRE="${1:?用法：wire-console-env.sh <WIRE_FILE> <CONSOLE_ENV_FILE> [KEY=VALUE ...]}"
ENVF="${2:?缺 CONSOLE_ENV_FILE}"
shift 2
[ -f "$WIRE" ] || { echo "FAIL: 接线清单缺失：$WIRE" >&2; exit 1; }
umask 177
touch "$ENVF"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cp "$ENVF" "$tmp"
merge_kv() {
  line="$1"
  case "$line" in ''|\#*) return 0 ;; esac
  k=${line%%=*}
  case "$k" in *[!A-Za-z0-9_]*|"") return 0 ;; esac  # 只接受安全键名；异常行跳过
  grep -v "^$k=" "$tmp" > "$tmp.n" 2>/dev/null || : > "$tmp.n"
  mv "$tmp.n" "$tmp"
  printf '%s\n' "$line" >> "$tmp"
}
while IFS= read -r line; do merge_kv "$line"; done < "$WIRE"
for kv in "$@"; do merge_kv "$kv"; done
mv "$tmp" "$ENVF"
chmod 600 "$ENVF"
