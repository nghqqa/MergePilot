#!/bin/bash
# provision-workers.sh — 四具名 Agent 的受控置备（AgentTeams Beta 部署清单固化）。
# 流程（全部经真实 Controller API；凭据零落盘）：
#  1. POST /api/v1/workers 创建 mergepilot-{leader,reviewer,fixer,verifier}
#     （controller 完成 Matrix 账号/Room/MinIO 用户/openclaw.json 推送）
#  2. 以 controller 生成的同一凭据集 + 共享网络命名空间接管 worker 容器
#     （上游 embedded 对 worker 注入 127.0.0.1 URL——仅 controller netns 内可达；
#      容器采用 controller 命名以被其认领与观测）
#  3. worker 模型直连配置（deepseek——上游 gateway AI-provider init 断链的规避，
#     openclaw.json 属 controller MinIO 凭据域，key 来自进程环境）
#  4. touch spec.state 触发 reconcile → 四 Agent phase=Running
# 前置：docker compose up -d 已完成且 controller healthy；ATB_* 环境变量在位。
set -eu
STACK=${ATB_STACK:-agentteams-beta}
CTRL=$STACK-ctrl
PORT=${ATB_API_PORT:-28462}
IMG=agentteams/copaw-worker:223ddc2-agentloop-v5fix
: "${ATB_ADMIN_PASSWORD:?ATB_ADMIN_PASSWORD required}"
: "${ATB_LLM_KEY:?ATB_LLM_KEY required (one-shot non-production)}"
case "$(uname -s)" in MINGW*|MSYS*) declare -f >/dev/null;; esac

TOK=$(docker exec $CTRL sh -c 'tr -d "\n\r" < /var/run/agentteams/cli-token')

for ROLE in leader reviewer fixer verifier; do
  NAME=mergepilot-$ROLE
  CTR=$STACK-worker-$NAME
  IDENTITY="MergePilot $ROLE"
  case "$ROLE" in
    leader)   IDENTITY="MergePilot 审查编排 Leader：只依据结构化 finding 摘要给出裁定建议，不执行任何写操作。";;
    reviewer) IDENTITY="MergePilot 审查 Reviewer：基于脱敏 finding 摘要给出语义审查建议（JSON）。";;
    fixer)    IDENTITY="MergePilot 修复 Fixer：仅产出 dry-run 修复建议文本，禁止执行命令或写仓库。";;
    verifier) IDENTITY="MergePilot 验证 Verifier：独立判断修复建议是否解决 finding，不信任 Fixer 自述。";;
  esac
  CONSOLE_PORT=$(( 28101 + $(case $ROLE in leader) echo 0;; reviewer) echo 1;; fixer) echo 2;; verifier) echo 3;; esac) ))

  # 1) 幂等创建（已存在 → 409 → 复用）
  CODE=$(curl -s -m 30 -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
    -d "{\"name\":\"$NAME\",\"spec\":{\"runtime\":\"copaw\",\"model\":\"deepseek-chat\",\"identity\":\"$IDENTITY\",\"state\":\"Running\"}}" \
    "http://127.0.0.1:$PORT/api/v1/workers" || echo 000)
  [ "$CODE" = "201" ] || [ "$CODE" = "409" ] || { echo "create $NAME failed: $CODE"; exit 1; }
  for i in $(seq 1 30); do [ -n "$(docker ps -aq --filter name=$CTR)" ] && break; sleep 2; done
  [ -n "$(docker ps -aq --filter name=$CTR)" ] || { echo "controller container for $NAME never appeared"; exit 1; }
  sleep 2

  # 2) 凭据采集 + 接管（同名/共享 netns/controller auth 卷/独立 console 端口/key 经 env）
  mapfile -t ENVARGS < <(docker inspect $CTR --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | tr -d '\r' | sed -E 's/^ +//; s/^/--env=/; /--env=$/d; /--env=AGENTTEAMS_CONSOLE_PORT=/d')
  AUTHVOL=$(MSYS_NO_PATHCONV=1 docker inspect $CTR --format '{{range .Mounts}}{{if eq .Destination "/var/run/secrets/agentteams"}}{{.Name}}{{end}}{{end}}')
  [ -n "$AUTHVOL" ] || { echo "auth volume not found for $NAME"; exit 1; }
  docker rm -f $CTR >/dev/null 2>&1
  MSYS_NO_PATHCONV=1 docker run -d --name $CTR --network container:$CTRL \
    --volume "$AUTHVOL:/var/run/secrets/agentteams" \
    --restart unless-stopped \
    "${ENVARGS[@]}" --env=AGENTTEAMS_CONSOLE_PORT=$CONSOLE_PORT \
    --env=DEEPSEEK_API_KEY="$ATB_LLM_KEY" $IMG >/dev/null

  # 3) 模型直连配置（MinIO 凭据域 openclaw.json；reconcile 覆盖后可重跑本脚本自愈）
  docker exec $CTR sh -c 'python3 - <<PYX
import json, subprocess, os
p = "agentteams/agentteams-storage/agents/" + os.environ["AGENTTEAMS_WORKER_NAME"] + "/openclaw.json"
raw = subprocess.run(["mc","cat",p],capture_output=True,text=True).stdout
d = json.loads(raw)
gw = d["models"]["providers"]["agentteams-gateway"]
d["models"]["providers"]["deepseek-direct"] = {
  "api": "openai-completions", "apiKey": os.environ["DEEPSEEK_API_KEY"],
  "baseUrl": "https://api.deepseek.com/v1",
  "models": [dict(m) for m in gw["models"] if m.get("id")=="deepseek-chat"] or [{"id":"deepseek-chat","name":"deepseek-chat","contextWindow":64000,"maxTokens":8000,"input":["text"]}]}
d["agents"]["defaults"]["model"]["primary"] = "deepseek-direct/deepseek-chat"
subprocess.run(["mc","pipe",p],input=json.dumps(d,ensure_ascii=False,indent=2),capture_output=True,text=True)
PYX' || { echo "model config patch failed for $NAME"; exit 1; }
  docker restart $CTR >/dev/null
  sleep 6
done

# 4) reconcile → phase Running（Sleeping→Running touch；幂等）
for ROLE in leader reviewer fixer verifier; do
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
    -d '{"spec":{"state":"Sleeping"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
    -d '{"spec":{"state":"Running"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
done
sleep 20
curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/api/v1/workers" \
  | python3 -c 'import json,sys; ws=json.load(sys.stdin)["workers"]; [print(w["name"], w["phase"]) for w in ws]; exit(0 if all(w["phase"]=="Running" for w in ws if w["name"].startswith("mergepilot-")) else 1)' \
  && echo "PROVISION-OK" || { echo "PROVISION-INCOMPLETE (re-run this script to self-heal)"; exit 1; }
