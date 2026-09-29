#!/bin/bash
# soak.sh — AgentTeams Beta 稳定性试运行（Wave 3.4 阶段 7 交付物）。
# 周期性只读探测 + 可选合成 review 触发；结果 JSONL 追加，判定标准见尾部。
# 用法：
#   INTERVAL=300 DURATION_H=8 [MERGEPILOT_BASE=http://127.0.0.1:48450] \
#   [WEBHOOK_URL=... WEBHOOK_SECRET=...] [OUT=soak.jsonl] bash soak.sh
# 说明：WEBHOOK_* 未提供时仅做健康/资源探测（不触发 review）。人工运行后按
# 判定标准评估——本脚本不宣称任何持续运行已完成。
set -u
STACK=${ATB_STACK:-agentteams-beta}
CTRL=$STACK-ctrl
PORT=${ATB_API_PORT:-28462}
INTERVAL=${INTERVAL:-300}
DURATION_H=${DURATION_H:-8}
OUT=${OUT:-soak.jsonl}
: "${MERGEPILOT_BASE:?MERGEPILOT_BASE required (MergePilot Beta base URL)}"
TOK=$(docker exec $CTRL sh -c 'tr -d "\n\r" < /var/run/agentteams/cli-token' 2>/dev/null || echo "")
END=$(( $(date +%s) + DURATION_H * 3600 ))
INIT_RESTARTS=$(for r in leader reviewer fixer verifier; do docker inspect -f '{{.RestartCount}}' $STACK-worker-mergepilot-$r 2>/dev/null || echo 0; done | paste -sd+ | bc)

echo "# soak start $(date -u +%FT%TZ) interval=${INTERVAL}s duration=${DURATION_H}h" >> "$OUT"
while [ "$(date +%s)" -lt "$END" ]; do
  TS=$(date -u +%FT%TZ)
  # 1) controller + 四 Agent（controller API）
  CTRL_OK=0; READY=0
  if [ -n "$TOK" ] && curl -sf -m 5 -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/api/v1/workers" >/tmp/atb-soak-workers.json 2>/dev/null; then
    CTRL_OK=1
    READY=$(python3 -c 'import json; ws=[w for w in json.load(open("/tmp/atb-soak-workers.json"))["workers"] if w["name"].startswith("mergepilot-")]; print(sum(1 for w in ws if w["phase"]=="Running"))' 2>/dev/null || echo 0)
  fi
  # 2) MergePilot 侧执行器状态（若提供 manage_instance 会话不可得则跳过——只读端点需登录；
  #    soak 主要以 controller 侧为准，此字段尽力而为）
  MP_STATE=unknown
  # 3) 容器资源/重启
  STATS=$(docker stats --no-stream --format "{{.Name}} {{.CPUPerc}} {{.MemUsage}}" 2>/dev/null | grep -E "$CTRL|worker-mergepilot" | tr '\n' ';' | sed 's/;$//')
  RESTARTS=$(for r in leader reviewer fixer verifier; do docker inspect -f '{{.RestartCount}}' $STACK-worker-mergepilot-$r 2>/dev/null || echo 0; done | paste -sd+ | bc)
  DISK=$(docker system df --format '{{.Type}} {{.Size}}' 2>/dev/null | grep Images | head -1)
  # 4) 可选合成 review（签名 webhook——需要真实 Beta 实例的凭据）
  TRIG=skipped
  if [ -n "${WEBHOOK_URL:-}" ] && [ -n "${WEBHOOK_SECRET:-}" ]; then
    BODY='{"action":"opened","installation":{"id":79001},"repository":{"id":99101,"full_name":"soak/repo"},"pull_request":{"number":900,"head":{"sha":"'"$(printf 'a%.0s' {1..40})"'"},"base":{"ref":"main"}}}'
    SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //')
    CODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "$WEBHOOK_URL" \
      -H 'content-type: application/json' -H 'x-github-event: pull_request' \
      -H "x-github-delivery: soak-$(date +%s)" -H "x-hub-signature-256: sha256=$SIG" -d "$BODY" || echo 000)
    TRIG="http_$CODE"
  fi
  printf '{"ts":"%s","ctrl_ok":%s,"workers_ready":%s,"restarts":%s,"trig":"%s","stats":"%s","disk":"%s"}\n' \
    "$TS" "$CTRL_OK" "$READY" "$RESTARTS" "$TRIG" "$STATS" "$DISK" >> "$OUT"
  sleep "$INTERVAL"
done
FINAL_RESTARTS=$(for r in leader reviewer fixer verifier; do docker inspect -f '{{.RestartCount}}' $STACK-worker-mergepilot-$r 2>/dev/null || echo 0; done | paste -sd+ | bc)
echo "# soak end $(date -u +%FT%TZ) restarts_total=$FINAL_RESTARTS (init=$INIT_RESTARTS)" >> "$OUT"

# 判定标准（人工评估 soak.jsonl）：
#  PASS 线：ctrl_ok=1 占比 ≥99%；workers_ready=4 占比 ≥99%；restarts_total-init ≤2；
#           docker stats 内存无单调增长（最后 25% 均值 ≤ 前 25% 均值 ×1.5）；触发类 http_2xx。
#  任一不满足 → 该 Beta 栈 NOT_READY（按 RUNBOOK §3 处置后重跑）。
