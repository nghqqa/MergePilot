#!/bin/sh
# patch-worker-cr-env.sh — Worker CR spec.env 幂等注入（2026-10-21 ACL 真源收口）。
#
# 背景：REST POST /api/v1/workers 创建的 worker 不携带 AGENTTEAMS_ADMIN_USER
# → qwenpaw ACL 预播回退泛化 "@admin:<domain>"（错 ID）→ 真实管理员消息落入
# pending（"需要审批"），授权消息往返被拒。注入点=Worker CR spec.env（经 ctrl
# 6443 k8s API merge-patch；REST 路径不认 spec.env——上游限制，2026-10-21 实测）。
# CR 持久于 ctrl 数据卷，容器重建/唤醒时按 CR 渲染 env——本补丁一次落库，
# 重建后持续生效，无需逐 worker 改文件。
#
# 用法（在宿主、Docker 可用环境执行）：
#   patch-worker-cr-env.sh                # 默认四 worker（mergepilot-*）
#   patch-worker-cr-env.sh <name> [...]   # 指定 worker
#   patch-worker-cr-env.sh --check        # 只读：汇报各 CR 当前 spec.env 状态
#
# 纪律：值取自 ctrl 自身 env（AGENTTEAMS_ADMIN_USER），不落地任何明文到本脚本输出。
set -eu
CTRL="${CTRL:-agentteams-beta-ctrl}"
NET="${NET:-agentteams-beta_atnet}"

read_ctrl_env() {
  docker exec "$CTRL" sh -c 'eval "v=\${$1}"; printf %s "$v"' sh "$1" 2>/dev/null
}

APU="$(read_ctrl_env AGENTTEAMS_ADMIN_USER)"
[ -n "$APU" ] || { echo "FATAL: ctrl env 缺 AGENTTEAMS_ADMIN_USER（真源未配置）" >&2; exit 1; }
RETRIES="$(read_ctrl_env QWENPAW_LLM_MAX_RETRIES)"; RETRIES="${RETRIES:-12}"
BACKOFF="$(read_ctrl_env QWENPAW_LLM_BACKOFF_BASE)"; BACKOFF="${BACKOFF:-3}"

if [ "$1" = "--check" ]; then
  docker exec "$CTRL" sh -c 'TOK=$(cut -d, -f1 /data/agentteams-beta-data/pki/token.csv 2>/dev/null || cut -d, -f1 /agentteams-beta-data/pki/token.csv 2>/dev/null); for w in mergepilot-leader mergepilot-reviewer mergepilot-fixer mergepilot-verifier; do R=$(curl -sk -m 10 --cacert /data/agentteams-beta-data/pki/ca.crt -H "Authorization: Bearer $TOK" "https://127.0.0.1:6443/apis/agentteams.io/v1beta1/namespaces/default/workers/$w"); echo "$w $(printf %s "$R" | grep -o "\"AGENTTEAMS_ADMIN_USER\":\"[^\"]*\"" || echo "spec.env:ABSENT")"; done'
  exit 0
fi

WORKERS="${*:-mergepilot-leader mergepilot-reviewer mergepilot-fixer mergepilot-verifier}"
for w in $WORKERS; do
  docker exec "$CTRL" sh -c "TOK=\$(cut -d, -f1 /data/agentteams-beta-data/pki/token.csv 2>/dev/null || cut -d, -f1 /agentteams-beta-data/pki/token.csv 2>/dev/null); R=\$(curl -sk -m 10 --cacert /data/agentteams-beta-data/pki/ca.crt -X PATCH -H 'Authorization: Bearer '\"\$TOK\" -H 'Content-Type: application/merge-patch+json' --data '{\"spec\":{\"env\":{\"AGENTTEAMS_ADMIN_USER\":\"$APU\",\"QWENPAW_LLM_MAX_RETRIES\":\"$RETRIES\",\"QWENPAW_LLM_BACKOFF_BASE\":\"$BACKOFF\"}}}' https://127.0.0.1:6443/apis/agentteams.io/v1beta1/namespaces/default/workers/$w); printf '%s %s\n' '$w' \"\$(printf %s \"\$R\" | grep -o '\"generation\":[0-9]*' | head -1)\""
done
echo "patched: $WORKERS"
echo "next: 触发容器重建（docker rm -f 旧容器后由 ctrl 按 CR 重建，或等下次唤醒）；重建后 env 即携带注入值。"
