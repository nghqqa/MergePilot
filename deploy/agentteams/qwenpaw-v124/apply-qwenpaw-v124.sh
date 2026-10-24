#!/usr/bin/env bash
# apply-qwenpaw-v124.sh — QwenPaw v1.2.4 候选栈可重复部署（隔离优先；生产切换另窗）。
# 真源：deploy/agentteams/qwenpaw-v124/MANIFEST-qwenpaw-v124.md（顺序即契约）。
# 凭据零入库：LLM key 等经 env 注入；栈初始化凭据（admin/registration/minio）首轮生成后
#   持久保存于 QP_SECRET_FILE（默认 ~/.qwp2-secrets.env，umask 077）——重建/重跑复用同值，
#   不产生"每次 apply 凭据漂移"（2026-10-11 纠偏：此前每轮随机生成导致重跑无法对账）。
# 幂等=收敛而非跳过：已存在容器核对 digest/挂载/关键配置后复用；provider/route 重跑先读
#   实际配置再收敛（2026-10-11 纠偏：不得把任意 409 当配置正确、不得忽略失败）。
set -euo pipefail

QP_CTRL_NAME="${QP_CTRL_NAME:-qwp2-controller}"
QP_NETWORK="${QP_NETWORK:-qwp2-net}"
QP_VOLUME="${QP_VOLUME:-qwp2-data}"
QP_PORT_CTRL="${QP_PORT_CTRL:-18190}"
QP_PORT_MATRIX="${QP_PORT_MATRIX:-18186}"
QP_PORT_MINIO="${QP_PORT_MINIO:-19100}"
QP_PORT_MGR_CONSOLE="${QP_PORT_MGR_CONSOLE:-18889}"   # 生产旧栈占 18888——错开
QP_ADMIN_USER="${QP_ADMIN_USER:-admin}"
QP_SECRET_FILE="${QP_SECRET_FILE:-$HOME/.qwp2-secrets.env}"
# FS 配置（controller 写对象存储的实际凭据面——2026-10-10 实测缺失即 replan 500）
FS_BUCKET="${FS_BUCKET:-agentteams-storage}"
QP_AUTH_TOKEN_FILE="${QP_AUTH_TOKEN_FILE:-/var/run/agentteams/cli-token}"

# LLM 上游：env 优先；未提供时从秘密文件读（首轮由 apply 写入）
: "${QP_LLM_BASE_URL:=$(grep -m1 '^QP_LLM_BASE_URL=' "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- || true)}"
: "${QP_LLM_API_KEY:=$(grep -m1 '^QP_LLM_API_KEY=' "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- || true)}"
: "${QP_LLM_MODEL:=$(grep -m1 '^QP_LLM_MODEL=' "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- || true)}"
: "${QP_LLM_BASE_URL:?需 QP_LLM_BASE_URL（如 https://api.deepseek.com）}"
: "${QP_LLM_API_KEY:?需 QP_LLM_API_KEY（秘密源注入，不入库）}"
: "${QP_LLM_MODEL:?需 QP_LLM_MODEL（如 deepseek-chat）}"

REG="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams"
IMG_CTRL="$REG/agentteams-embedded@sha256:d96eb4dd4cef0ba8f02cfea4bb114d00f248cd8ecac0b2ce909150bd3e56a289"
IMG_MGR="$REG/agentteams-manager-qwenpaw@sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805"
IMG_WORKER_COPAW="$REG/agentteams-copaw-worker@sha256:25e10515a61ed7c877c1993e515a7b27064bbc87c7eab9248ae7a7c1198a147a"
IMG_WORKER_QP="$REG/agentteams-qwenpaw-worker@sha256:4f91b1026fe116f87c639f19e92f7e1f26f59fb408a90425691c32f700c3a5f5"

fail() { echo "FAIL: $*" >&2; exit 1; }
# docker exec/run 的 argv 含容器路径——MSYS 转换全程关闭；宿主侧 curl 的 /dev/null
# 一律用 shell 级重定向（bash 自身处理，不受本变量影响）——2026-10-11 定案
export MSYS_NO_PATHCONV=1
log() { echo "[apply] $*"; }

# ── 0. 栈凭据：秘密文件持久化（首轮生成，重建复用；显式 env 优先并回写） ──
touch "$QP_SECRET_FILE" && chmod 0700 "$QP_SECRET_FILE"
get_secret() { grep -m1 "^$1=" "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- || true; }
put_secret() { # $1=key $2=value（去重写入）
  local k="$1" v="$2"
  grep -v "^$k=" "$QP_SECRET_FILE" > "$QP_SECRET_FILE.tmp" 2>/dev/null || true
  echo "$k=$v" >> "$QP_SECRET_FILE.tmp" && mv "$QP_SECRET_FILE.tmp" "$QP_SECRET_FILE"
  chmod 0700 "$QP_SECRET_FILE"
}
QP_ADMIN_PASSWORD="${QP_ADMIN_PASSWORD:-$(get_secret QP_ADMIN_PASSWORD)}"
QP_REGISTRATION_TOKEN="${QP_REGISTRATION_TOKEN:-$(get_secret QP_REGISTRATION_TOKEN)}"
QP_MINIO_USER="${QP_MINIO_USER:-$(get_secret QP_MINIO_USER)}"
QP_MINIO_PASSWORD="${QP_MINIO_PASSWORD:-$(get_secret QP_MINIO_PASSWORD)}"
[ -n "$QP_ADMIN_PASSWORD" ]      || QP_ADMIN_PASSWORD="qwp2-admin-$(openssl rand -hex 16)"
[ -n "$QP_REGISTRATION_TOKEN" ]  || QP_REGISTRATION_TOKEN="qwp2-reg-$(openssl rand -hex 12)"
[ -n "$QP_MINIO_USER" ]          || QP_MINIO_USER=qwp2-minio
[ -n "$QP_MINIO_PASSWORD" ]      || QP_MINIO_PASSWORD="qwp2-minio-$(openssl rand -hex 16)"
put_secret QP_ADMIN_PASSWORD "$QP_ADMIN_PASSWORD"
put_secret QP_REGISTRATION_TOKEN "$QP_REGISTRATION_TOKEN"
put_secret QP_MINIO_USER "$QP_MINIO_USER"
put_secret QP_MINIO_PASSWORD "$QP_MINIO_PASSWORD"
put_secret QP_LLM_BASE_URL "$QP_LLM_BASE_URL"
put_secret QP_LLM_MODEL "$QP_LLM_MODEL"
put_secret QP_LLM_API_KEY "$QP_LLM_API_KEY"
log "栈凭据持久化于 $QP_SECRET_FILE（0700；重建复用同值）"

# ── 1. 预检：端口占用区分本栈（同前缀容器）vs 其他进程 ──
command -v docker >/dev/null || fail "docker 不可达"
CTRL_CID=$(docker ps -a --format '{{.ID}}' --filter "name=^$QP_CTRL_NAME$" 2>/dev/null || true)
STACK_PREFIX="${QP_CTRL_NAME%-controller}"   # qwp2-controller → qwp2（manager/worker 同前缀视为本栈）
for p in "$QP_PORT_CTRL" "$QP_PORT_MATRIX" "$QP_PORT_MINIO" "$QP_PORT_MGR_CONSOLE"; do
  if netstat -ano 2>/dev/null | grep "127.0.0.1:$p " | grep -q LISTENING; then
    HOLDER=$(docker ps --format '{{.Names}}' | while read -r n; do docker port "$n" 2>/dev/null | grep -q ":$p" && echo "$n" && break; done)
    if [ -n "$HOLDER" ]; then
      case "$HOLDER" in
        "$STACK_PREFIX"*) log "端口 $p 由本栈容器 $HOLDER 占用（复用场景，放行）";;
        *) fail "端口 127.0.0.1:$p 被 $HOLDER 占用（非本栈）——换 QP_PORT_* 或释放后重试（不猜、不覆盖）";;
      esac
    else
      fail "端口 127.0.0.1:$p 被非 Docker 进程占用——换 QP_PORT_* 或释放后重试"
    fi
  fi
done
log "预检通过（网络/端口/env/凭据）"

# ── 2. 隔离资源（幂等） ──
docker network create "$QP_NETWORK" >/dev/null 2>&1 || true
docker volume create "$QP_VOLUME" >/dev/null
log "网络/卷就绪：$QP_NETWORK / $QP_VOLUME"

# ── 3. controller：已存在则核对（digest/卷/关键 env），不匹配即失败；不存在则创建 ──
if docker ps -a --format '{{.Names}}' | grep -qx "$QP_CTRL_NAME"; then
  [ -n "$CTRL_CID" ] || fail "容器 $QP_CTRL_NAME 存在但不可用（Exited？）——docker rm $QP_CTRL_NAME 后重跑"
  CUR_IMG=$(docker inspect "$QP_CTRL_NAME" --format '{{.Image}}')
  WANT_IMG=$(docker image inspect "$IMG_CTRL" --format '{{.Id}}' 2>/dev/null || fail "锁定镜像未拉取：$IMG_CTRL")
  [ "$CUR_IMG" = "$WANT_IMG" ] || fail "已存在容器镜像与锁定 digest 不符（CUR=$CUR_IMG）——需显式重建（删容器+保留卷数据快照）"
  CUR_VOL=$(docker inspect "$QP_CTRL_NAME" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
  [ "$CUR_VOL" = "$QP_VOLUME" ] || fail "已存在容器数据卷不符（CUR=$CUR_VOL 期望=$QP_VOLUME）——拒绝静默换卷"
  for kv in "AGENTTEAMS_ADMIN_PASSWORD=$QP_ADMIN_PASSWORD" "AGENTTEAMS_MINIO_USER=$QP_MINIO_USER"; do
    k=${kv%%=*}; v=${kv#*=}
    CUR_V=$(docker inspect "$QP_CTRL_NAME" --format "{{range .Config.Env}}{{println .}}{{end}}" | grep "^$k=" | cut -d= -f2- || true)
    [ "$CUR_V" = "$v" ] || fail "已存在容器 env $k 与秘密文件不一致——凭据漂移（先对账再重建）"
  done
  docker start "$QP_CTRL_NAME" >/dev/null 2>&1 || true
  log "已存在 controller 核对通过（镜像/卷/凭据一致）——复用"
else
  AUTH_TOKEN_ARGS=()
  [ -n "${QP_API_TOKEN:-}" ] && AUTH_TOKEN_ARGS=(-e "AGENTTEAMS_AUTH_TOKEN=$QP_API_TOKEN")
  MSYS_NO_PATHCONV=1 docker run -d --name "$QP_CTRL_NAME" \
    --hostname "$QP_CTRL_NAME" --network "$QP_NETWORK" --restart no \
    "${AUTH_TOKEN_ARGS[@]}" \
    -e AGENTTEAMS_CONTROLLER_URL="http://$QP_CTRL_NAME:8090" \
    -e AGENTTEAMS_DOCKER_NETWORK="$QP_NETWORK" \
    -e AGENTTEAMS_DATA_DIR=/data \
    -e AGENTTEAMS_RESOURCE_PREFIX=qwp2- \
    -e AGENTTEAMS_WORKSPACE_DIR=/tmp/qwp2-ws \
    -e AGENTTEAMS_HOST_SHARE_DIR=/tmp/qwp2-host \
    -e AGENTTEAMS_MATRIX_URL="http://$QP_CTRL_NAME:6167" \
    -e AGENTTEAMS_AI_GATEWAY_DOMAIN="$QP_CTRL_NAME" \
    -e AGENTTEAMS_AI_GATEWAY_URL="http://$QP_CTRL_NAME:8080" \
    -e AGENTTEAMS_AI_GATEWAY_ADMIN_URL="http://$QP_CTRL_NAME:8001" \
    -e AGENTTEAMS_WORKER_IDLE_TIMEOUT=720 \
    -e AGENTTEAMS_AUTH_TOKEN_FILE="$QP_AUTH_TOKEN_FILE" \
    -e AGENTTEAMS_MATRIX_APPSERVICE_ENABLED=false \
    -e AGENTTEAMS_ADMIN_USER="$QP_ADMIN_USER" \
    -e AGENTTEAMS_ADMIN_PASSWORD="$QP_ADMIN_PASSWORD" \
    -e AGENTTEAMS_REGISTRATION_TOKEN="$QP_REGISTRATION_TOKEN" \
    -e AGENTTEAMS_LLM_PROVIDER=openai-compat \
    -e AGENTTEAMS_OPENAI_BASE_URL="$QP_LLM_BASE_URL" \
    -e AGENTTEAMS_OPENAI_API_KEY="$QP_LLM_API_KEY" \
    -e AGENTTEAMS_DEFAULT_MODEL="$QP_LLM_MODEL" \
    -e AGENTTEAMS_FS_ENDPOINT="http://$QP_CTRL_NAME:9000" \
    -e AGENTTEAMS_FS_ACCESS_KEY="$QP_MINIO_USER" \
    -e AGENTTEAMS_FS_SECRET_KEY="$QP_MINIO_PASSWORD" \
    -e AGENTTEAMS_FS_BUCKET="$FS_BUCKET" \
    -e AGENTTEAMS_DEFAULT_WORKER_RUNTIME=copaw \
    -e AGENTTEAMS_MANAGER_RUNTIME=qwenpaw \
    -e AGENTTEAMS_PORT_MANAGER_CONSOLE="$QP_PORT_MGR_CONSOLE" \
    -e AGENTTEAMS_MANAGER_IMAGE="$IMG_MGR" \
    -e AGENTTEAMS_WORKER_IMAGE="$IMG_WORKER_COPAW" \
    -e AGENTTEAMS_COPAW_WORKER_IMAGE="$IMG_WORKER_COPAW" \
    -e AGENTTEAMS_QWENPAW_WORKER_IMAGE="$IMG_WORKER_QP" \
    -e AGENTTEAMS_MINIO_USER="$QP_MINIO_USER" \
    -e AGENTTEAMS_MINIO_PASSWORD="$QP_MINIO_PASSWORD" \
    -e AGENTTEAMS_DASHBOARD=0 \
    -v "$QP_VOLUME:/data" \
    -v "${QP_DOCKER_SOCK:-/run/host-services/docker.proxy.sock}:/var/run/docker.sock" \
    -p "127.0.0.1:$QP_PORT_CTRL:8090" -p "127.0.0.1:$QP_PORT_MATRIX:6167" -p "127.0.0.1:$QP_PORT_MINIO:9000" \
    "$IMG_CTRL" >/dev/null
  log "controller 已创建（digest 锁定；FS endpoint/凭据已注入）"
fi

# ── 4. 健康等待 + cli-token ──
log "等待 controller 健康 + cli-token（最长 180s）..."
DEADLINE=$(( $(date +%s) + 180 ))
until curl -sf "http://127.0.0.1:$QP_PORT_MATRIX/" >/dev/null && \
      docker exec "$QP_CTRL_NAME" sh -c 'test -s /var/run/agentteams/cli-token' 2>/dev/null; do
  [ "$(date +%s)" -lt "$DEADLINE" ] || fail "controller 健康等待超时（查 docker logs $QP_CTRL_NAME）"
  sleep 5
done
TOK=$(docker exec "$QP_CTRL_NAME" sh -c 'tr -d "\r\n" < /var/run/agentteams/cli-token')
log "controller 健康（token 已落盘）"

# ── 5. Higress provider/route：先读实际配置再收敛（读-比对-写-验证；失败不吞） ──
DOMAIN=$(printf '%s' "$QP_LLM_BASE_URL" | sed -E 's#^https?://##; s#[:/].*##')
PORT=443; PROTO=https
case "$QP_LLM_BASE_URL" in http:*) PORT=80; PROTO=http;; esac
# hgc：宿主侧转发全部参数进容器内 curl（"$@" 必须显式前移——2026-10-11 审阅纠偏：
# 此前定义漏转发函数参数，URL 被丢弃、密码被当 URL，配合 set -e 静默退出）
hgc() { docker exec "$QP_CTRL_NAME" sh -c 'u="$1"; shift; curl -s -u "admin:$u" "$@"' _ "$QP_ADMIN_PASSWORD" "$@"; }
# 5a. service-sources：读实际 → 缺失才建；存在则核字段
# 读回码提取：取尾部 3 字符（curl -w 输出码；管道失败兜底不再拼串——纠偏 2026-10-11）
read_code() { printf '%s' "$1" | tail -c 3; }
SS_RAW=$(hgc "http://127.0.0.1:8001/v1/service-sources/openai-compat" -o /tmp/ss-cur.json -w '%{http_code}' 2>/dev/null || true)
SS_CUR=$(read_code "$SS_RAW")
if [ "$SS_CUR" = "200" ]; then
  docker exec "$QP_CTRL_NAME" grep -q "\"$DOMAIN\"" /tmp/ss-cur.json 2>/dev/null || fail "service-sources/openai-compat 已存在但域名不符（期望 $DOMAIN）——人工对账"
  log "service-sources 已存在且域名一致——复用"
else
  JSON_SS=$(printf '{"type":"dns","name":"openai-compat","port":"%s","protocol":"%s","proxyName":"","domain":"%s"}' "$PORT" "$PROTO" "$DOMAIN")
  CODE_SS_RAW=$(hgc -X POST "http://127.0.0.1:8001/v1/service-sources" -H 'content-type: application/json' --data-raw "$JSON_SS" -o /tmp/ss-put.json -w '%{http_code}' || true)
  CODE_SS=$(read_code "$CODE_SS_RAW")
  case "$CODE_SS" in
    200|201) log "service-sources 创建（$CODE_SS）";;
    409) # 已存在——重读核内容（收敛语义：409 不得静默当正确）
      hgc "http://127.0.0.1:8001/v1/service-sources/openai-compat" -o /tmp/ss-cur.json -w '' >/dev/null 2>&1 || true
      docker exec "$QP_CTRL_NAME" grep -q "\"$DOMAIN\"" /tmp/ss-cur.json 2>/dev/null || fail "service-sources 409 且重读不符（期望 $DOMAIN）：$(docker exec "$QP_CTRL_NAME" head -c 150 /tmp/ss-cur.json 2>/dev/null)"
      log "service-sources 已存在（409）且域名核对一致——复用";;
    *) fail "service-sources 创建失败（$CODE_SS）：$(head -c 150 /tmp/ss-put.json 2>/dev/null)";;
  esac
fi
sleep 2
# 5b. ai/providers：读实际 → 比对 baseUrl+token 形状 → PUT 收敛；写后回读验证（不吞 409/不猜）
PROV_RAW=$(hgc "http://127.0.0.1:8001/v1/ai/providers/openai-compat" -o /tmp/prov-cur.json -w '%{http_code}' 2>/dev/null || true)
PROV_CUR=$(read_code "$PROV_RAW")
NEED_PROV=1
if [ "$PROV_CUR" = "200" ]; then
  URL_OK=0; TOK_OK=0
  docker exec "$QP_CTRL_NAME" sh -c 'grep -q "\"$0\"" /tmp/prov-cur.json' "$QP_LLM_BASE_URL" && URL_OK=1
  docker exec "$QP_CTRL_NAME" sh -c 'grep -q "\"$0\"" /tmp/prov-cur.json' "$QP_LLM_API_KEY" && TOK_OK=1
  if [ "$URL_OK" = "1" ] && [ "$TOK_OK" = "1" ]; then
    NEED_PROV=0; log "ai/providers 实际配置与期望一致（baseUrl+token）——复用"
  elif [ "$URL_OK" = "1" ]; then
    log "ai/providers baseUrl 一致但 token 不同——将 PUT 收敛"
  else
    log "ai/providers 实际 baseUrl 不同——将 PUT 收敛"
  fi
fi
if [ "$NEED_PROV" = "1" ]; then
  JSON_PROV=$(printf '{"type":"openai","name":"openai-compat","tokens":["%s"],"version":0,"protocol":"openai/v1","tokenFailoverConfig":{"enabled":false},"rawConfigs":{"openaiCustomUrl":"%s","openaiCustomServiceName":"openai-compat.dns","openaiCustomServicePort":%s}}' "$QP_LLM_API_KEY" "$QP_LLM_BASE_URL" "$PORT")
  CODE_P=$(hgc -X POST "http://127.0.0.1:8001/v1/ai/providers" -H 'content-type: application/json' --data-raw "$JSON_PROV" -o /tmp/prov-put.json -w '%{http_code}')
  if [ "$CODE_P" != "200" ] && [ "$CODE_P" != "201" ]; then
    JSON_PROV2=${JSON_PROV/"\"version\":0"/'"version":1'}
    CODE_P2=$(hgc -X PUT "http://127.0.0.1:8001/v1/ai/providers/openai-compat" -H 'content-type: application/json' --data-raw "$JSON_PROV2" -o /tmp/prov-put2.json -w '%{http_code}')
    [ "$CODE_P2" = "200" ] || fail "ai/providers 收敛失败（POST=$CODE_P PUT=$CODE_P2）：$(head -c 150 /tmp/prov-put2.json 2>/dev/null)"
  fi
  # 写后回读验证（token/baseUrl 真实落位——409/跳过不等于正确）
  hgc "http://127.0.0.1:8001/v1/ai/providers/openai-compat" -o /tmp/prov-verify.json -w 'providers readback: %{http_code}\n' >/dev/null
  docker exec "$QP_CTRL_NAME" sh -c 'grep -q "\"$0\"" /tmp/prov-verify.json && grep -q "\"$1\"" /tmp/prov-verify.json' "$QP_LLM_BASE_URL" "$QP_LLM_API_KEY" \
    || fail "providers 写后回读不符（baseUrl/token 未落位）——拒绝继续"
  log "ai/providers 收敛并回读验证通过"
fi
# 5c. ai/routes：读实际 → 缺失/不符才写（matchType=PRE 大写 + matchValue 契约）
ROUTE_OK=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -u "admin:$0" "http://127.0.0.1:8001/v1/ai/routes/mp-llm" | grep -c "openai-compat"' "$QP_ADMIN_PASSWORD" 2>/dev/null || echo 0)
if [ "${ROUTE_OK:-0}" -ge 1 ]; then
  log "ai/routes/mp-llm 已存在且指向 openai-compat——复用"
else
  JSON_ROUTE='{"name":"mp-llm","domains":["*"],"pathPredicate":{"matchType":"PRE","matchValue":"/v1/chat/completions"},"upstreams":[{"provider":"openai-compat","weight":100}],"version":0}'
  CODE_R=$(hgc -X POST "http://127.0.0.1:8001/v1/ai/routes" -H 'content-type: application/json' --data-raw "$JSON_ROUTE" -o /tmp/route-put.json -w '%{http_code}')
  case "$CODE_R" in 200|201) log "AI route 创建（$CODE_R）";; *) fail "AI route 播种失败（$CODE_R）：$(head -c 150 /tmp/route-put.json 2>/dev/null)——契约 matchType=PRE 大写+matchValue";; esac
fi

# ── 6. 网关真实 LLM 探测（fail-closed；文件化载荷防引号变形 + 重试覆盖 envoy 同步滞后）──
# 2026-10-24 实证：route 创建后 envoy 配置传播有秒级滞后（一次性探测 404 假失败）；
# 内联 JSON 经 sh -c 双层转义易变形（400）——载荷落文件 + --data-binary。
docker exec "$QP_CTRL_NAME" sh -c 'printf "%s" "{\"model\":\"$0\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" > /tmp/gwprobe-req.json' "$QP_LLM_MODEL"
GW=000
for _try in $(seq 1 18); do
  GW=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /tmp/gwprobe.json -w "%{http_code}" -X POST http://127.0.0.1:8080/v1/chat/completions -H "content-type: application/json" -H "Authorization: Bearer probe" --data-binary @/tmp/gwprobe-req.json')
  [ "$GW" = "200" ] && break
  sleep 5
done
[ "$GW" = "200" ] || fail "网关 LLM 探测失败（HTTP $GW，重试 90s）——检查 provider/route/上游 key（docker exec $QP_CTRL_NAME cat /tmp/gwprobe.json 脱敏查看）"
log "网关真实 LLM 探测 200（上游路由打通）"

cat <<EOF

[apply 完成] 后续步骤：
  * 具名 worker/team 置备：走 Console 执行器链（MergePilot executor ≥ v1.2.4 双代契约适配版）
  * 验收：QP_CTRL_NAME=$QP_CTRL_NAME QP_CTRL_PORT=$QP_PORT_CTRL QP_ADMIN_PASSWORD=\$QP_ADMIN_PASSWORD bash verify-qwenpaw-v124.sh
  * 凭据：$QP_SECRET_FILE（0700，重建复用；勿入库勿提交）
EOF
