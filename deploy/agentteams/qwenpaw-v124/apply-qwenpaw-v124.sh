#!/usr/bin/env bash
# apply-qwenpaw-v124.sh — QwenPaw v1.2.4 候选栈可重复部署（隔离优先；生产切换另窗）。
# 真源：deploy/agentteams/qwenpaw-v124/MANIFEST-qwenpaw-v124.md（顺序即契约）。
# 凭据零入库：全部经 env 注入（QP_LLM_API_KEY 等必填，缺失即失败——不猜默认值）。
# 幂等：重复运行收敛到同一期望态（已存在资源跳过）。
set -euo pipefail

# ── 必填 env（缺失即失败） ──
: "${QP_LLM_BASE_URL:?需 QP_LLM_BASE_URL（如 https://api.deepseek.com）}"
: "${QP_LLM_API_KEY:?需 QP_LLM_API_KEY（秘密源注入，不入库）}"
: "${QP_LLM_MODEL:?需 QP_LLM_MODEL（如 deepseek-chat）}"
: "${QP_ADMIN_PASSWORD:?需 QP_ADMIN_PASSWORD（隔离栈管理凭据，全新生成不复用生产）}"
: "${QP_REGISTRATION_TOKEN:?需 QP_REGISTRATION_TOKEN}"

QP_CTRL_NAME="$QP_CTRL_NAME"
QP_NETWORK="${QP_NETWORK:-qwp2-net}"
QP_VOLUME="${QP_VOLUME:-qwp2-data}"
QP_PORT_CTRL="${QP_PORT_CTRL:-18190}"
QP_PORT_MATRIX="${QP_PORT_MATRIX:-18186}"
QP_PORT_MINIO="${QP_PORT_MINIO:-19100}"
QP_PORT_MGR_CONSOLE="${QP_PORT_MGR_CONSOLE:-18889}"   # 生产旧栈占 18888——错开
QP_ADMIN_USER="${QP_ADMIN_USER:-admin}"

REG="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams"
IMG_CTRL="$REG/agentteams-embedded@sha256:d96eb4dd4cef0ba8f02cfea4bb114d00f248cd8ecac0b2ce909150bd3e56a289"
IMG_MGR="$REG/agentteams-manager-qwenpaw@sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805"
IMG_WORKER_COPAW="$REG/agentteams-copaw-worker@sha256:25e10515a61ed7c877c1993e515a7b27064bbc87c7eab9248ae7a7c1198a147a"
IMG_WORKER_QP="$REG/agentteams-qwenpaw-worker@sha256:4f91b1026fe116f87c639f19e92f7e1f26f59fb408a90425691c32f700c3a5f5"

fail() { echo "FAIL: $*" >&2; exit 1; }
log() { echo "[apply] $*"; }

# ── 1. 预检 ──
command -v docker >/dev/null || fail "docker 不可达"
for p in "$QP_PORT_CTRL" "$QP_PORT_MATRIX" "$QP_PORT_MINIO" "$QP_PORT_MGR_CONSOLE"; do
  if netstat -ano 2>/dev/null | grep -q "127.0.0.1:$p "; then fail "端口 127.0.0.1:$p 已被占用"; fi
done
log "预检通过（网络/端口/env）"

# ── 2. 隔离资源（幂等） ──
docker network create "$QP_NETWORK" >/dev/null 2>&1 || true
docker volume create "$QP_VOLUME" >/dev/null
log "网络/卷就绪：$QP_NETWORK / $QP_VOLUME"

# ── 3. controller（digest 锁定） ──
if docker ps --format '{{.Names}}' | grep -qx $QP_CTRL_NAME; then
  log "controller 已在运行——跳过（幂等；如需重建先手工移除）"
else
  docker rm -f $QP_CTRL_NAME >/dev/null 2>&1 || true
  export MSYS_NO_PATHCONV=1
  docker run -d --name $QP_CTRL_NAME \
    --hostname $QP_CTRL_NAME --network "$QP_NETWORK" --restart no \
    -e AGENTTEAMS_CONTROLLER_URL="http://$QP_CTRL_NAME:8090" \
    -e AGENTTEAMS_DOCKER_NETWORK="$QP_NETWORK" \
    -e AGENTTEAMS_DATA_DIR=/data \
    -e AGENTTEAMS_RESOURCE_PREFIX=qwp2- \
    -e AGENTTEAMS_WORKSPACE_DIR=/tmp/qwp2-ws \
    -e AGENTTEAMS_HOST_SHARE_DIR=/tmp/qwp2-host \
    -e AGENTTEAMS_MATRIX_URL="http://$QP_CTRL_NAME:6167" \
    -e AGENTTEAMS_AI_GATEWAY_DOMAIN=$QP_CTRL_NAME \
    -e AGENTTEAMS_WORKER_IDLE_TIMEOUT=720 \
    -e AGENTTEAMS_AUTH_TOKEN_FILE=/var/run/agentteams/cli-token \
    -e AGENTTEAMS_MATRIX_APPSERVICE_ENABLED=false \
    -e AGENTTEAMS_ADMIN_USER="$QP_ADMIN_USER" \
    -e AGENTTEAMS_ADMIN_PASSWORD="$QP_ADMIN_PASSWORD" \
    -e AGENTTEAMS_REGISTRATION_TOKEN="$QP_REGISTRATION_TOKEN" \
    -e AGENTTEAMS_LLM_PROVIDER=openai-compat \
    -e AGENTTEAMS_OPENAI_BASE_URL="$QP_LLM_BASE_URL" \
    -e AGENTTEAMS_OPENAI_API_KEY="$QP_LLM_API_KEY" \
    -e AGENTTEAMS_DEFAULT_MODEL="$QP_LLM_MODEL" \
    -e AGENTTEAMS_DEFAULT_WORKER_RUNTIME=copaw \
    -e AGENTTEAMS_MANAGER_RUNTIME=qwenpaw \
    -e AGENTTEAMS_PORT_MANAGER_CONSOLE="$QP_PORT_MGR_CONSOLE" \
    -e AGENTTEAMS_MANAGER_IMAGE="$IMG_MGR" \
    -e AGENTTEAMS_WORKER_IMAGE="$IMG_WORKER_COPAW" \
    -e AGENTTEAMS_COPAW_WORKER_IMAGE="$IMG_WORKER_COPAW" \
    -e AGENTTEAMS_QWENPAW_WORKER_IMAGE="$IMG_WORKER_QP" \
    -e AGENTTEAMS_MINIO_USER=qwp2-minio \
    -e AGENTTEAMS_MINIO_PASSWORD="qwp2-minio-$(openssl rand -hex 8)" \
    -e AGENTTEAMS_DASHBOARD=0 \
    -v "$QP_VOLUME:/data" \
    -v /run/host-services/docker.proxy.sock:/var/run/docker.sock \
    -p "127.0.0.1:$QP_PORT_CTRL:8090" -p "127.0.0.1:$QP_PORT_MATRIX:6167" -p "127.0.0.1:$QP_PORT_MINIO:9000" \
    "$IMG_CTRL" >/dev/null
  log "controller 已启动（digest 锁定）"
fi

# ── 4. 健康等待 + cli-token ──
log "等待 controller 健康 + cli-token（最长 180s）..."
DEADLINE=$(( $(date +%s) + 180 ))
until curl -sf -o /dev/null "http://127.0.0.1:$QP_PORT_MATRIX/" && \
      docker exec "$QP_CTRL_NAME" sh -c 'test -s /var/run/agentteams/cli-token' 2>/dev/null; do
  [ "$(date +%s)" -lt "$DEADLINE" ] || fail "controller 健康等待超时（查 docker logs "$QP_CTRL_NAME"）"
  sleep 5
done
TOK=$(docker exec "$QP_CTRL_NAME" sh -c 'tr -d "\r\n" < /var/run/agentteams/cli-token')
log "controller 健康即可用（token 已落盘）"

# ── 5. Higress provider + AI route 播种（幂等；argv 传参——容器内不二次展开，key 不回显） ──
DOMAIN=$(printf '%s' "$QP_LLM_BASE_URL" | sed -E 's#^https?://##; s#[:/].*##')
PORT=443; PROTO=https
case "$QP_LLM_BASE_URL" in http:*) PORT=80; PROTO=http;; esac
JSON_SRC=$(printf '{"type":"dns","name":"openai-compat","port":"%s","protocol":"%s","proxyName":"","domain":"%s"}' "$PORT" "$PROTO" "$DOMAIN")
docker exec "$QP_CTRL_NAME" sh -c 'curl -s -u "admin:$1" -X POST http://127.0.0.1:8001/v1/service-sources -H "content-type: application/json" --data-raw "$2" -o /dev/null -w "service-sources: %{http_code}\n"' _ "$QP_ADMIN_PASSWORD" "$JSON_SRC" || true
sleep 2
JSON_PROV=$(printf '{"type":"openai","name":"openai-compat","tokens":["%s"],"version":0,"protocol":"openai/v1","tokenFailoverConfig":{"enabled":false},"rawConfigs":{"openaiCustomUrl":"%s","openaiCustomServiceName":"openai-compat.dns","openaiCustomServicePort":%s}}' "$QP_LLM_API_KEY" "$QP_LLM_BASE_URL" "$PORT")
CODE_P=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -u "admin:$1" -X POST http://127.0.0.1:8001/v1/ai/providers -H "content-type: application/json" --data-raw "$2" -o /dev/null -w "%{http_code}"' _ "$QP_ADMIN_PASSWORD" "$JSON_PROV")
case "$CODE_P" in 200|201) log "ai/providers: $CODE_P";; *)
  JSON_PROV2=${JSON_PROV/"\"version\":0"/'"version":1'}
  CODE_P2=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -u "admin:$1" -X PUT http://127.0.0.1:8001/v1/ai/providers/openai-compat -H "content-type: application/json" --data-raw "$2" -o /dev/null -w "%{http_code}"' _ "$QP_ADMIN_PASSWORD" "$JSON_PROV2")
  [ "$CODE_P2" = "200" ] || fail "ai/providers 播种失败（POST=$CODE_P PUT=$CODE_P2）";;
esac
JSON_ROUTE='{"name":"mp-llm","domains":["*"],"pathPredicate":{"matchType":"PRE","matchValue":"/v1/chat/completions"},"upstreams":[{"provider":"openai-compat","weight":100}],"version":0}'
CODE_R=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -u "admin:$1" -X POST http://127.0.0.1:8001/v1/ai/routes -H "content-type: application/json" --data-raw "$2" -o /dev/null -w "%{http_code}"' _ "$QP_ADMIN_PASSWORD" "$JSON_ROUTE")
case "$CODE_R" in 200|201|409) log "AI route 就绪（$CODE_R）";; *) fail "AI route 播种失败（$CODE_R）——注意 v1.2.4 console 契约 matchType=PRE 大写+matchValue（2026-10-10 实证）";; esac

# ── 6. 网关真实 LLM 探测（fail-closed——漏配即 manager welcome 404 循环） ──
GW=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /tmp/gwprobe.json -w "%{http_code}" -X POST http://127.0.0.1:8080/v1/chat/completions -H "content-type: application/json" -H "Authorization: Bearer probe" -d "{\"model\":\"'$QP_LLM_MODEL'\",\"messages\":[{\"role\":\"user\",\"content\":\"回答一个字：好\"}]}"')
[ "$GW" = "200" ] || fail "网关 LLM 探测失败（HTTP $GW）——检查 provider/route/上游 key（docker exec "$QP_CTRL_NAME" cat /tmp/gwprobe.json 脱敏查看 errcode）"
log "网关真实 LLM 探测 200（上游路由打通）"

# ── 7. 摘要 ──
cat <<EOF

[apply 完成] 后续步骤：
  * 具名 worker/team 置备：走 Console 执行器链（MergePilot executor ≥ 含 v1.2.4 双代契约适配的版本），
    或 docker exec "$QP_CTRL_NAME" agt ...（cli-token 已就位）
  * 验收：bash verify-qwenpaw-v124.sh（health/workers/网关/mp_task 往返）
  * 组件版本：controller/manager/worker 全 digest 锁定（见 MANIFEST-qwenpaw-v124.md §1）
  * 凭据位置：env 注入面（QP_*）；本机不留痕（历史命令含 env 值者自行清理 shell 历史）
EOF
