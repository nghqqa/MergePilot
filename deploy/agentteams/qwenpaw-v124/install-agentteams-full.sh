#!/usr/bin/env bash
# install-agentteams-full.sh — QwenPaw v1.2.4 完整执行器栈统一安装入口（#520 波）。
#
# 流水线（顺序即契约；幂等可重跑，不覆盖已有身份/配置）：
#   1. 依赖初始化   三镜像 digest 核验（缺失即拉取；拉取后必核验）
#   2. ctrl/manager 就绪   调 apply-qwenpaw-v124.sh（含 controller 健康+Higress 播种+网关探测）
#   3. 四 worker 置备   经 ctrl API（union body；幂等=列表判存在→缺者建/在者对齐→读回拒漂移）
#                       ——与 console 执行器 ensureFourAgents 同一契约（agentteams-executor.mjs）
#   4. CR env 校验（受控幂等）  核对 ctrl env 契约（worker 镜像锁/runtime/网关/FS）——缺失或漂移即失败
#   5. console 接线清单  输出 console 侧所需 env（TOKEN 零回显原则：仅写文件 0600）
#   6. 健康验收   verify-qwenpaw-v124.sh
#
# 用法：
#   QP_LLM_BASE_URL=... QP_LLM_API_KEY=... QP_LLM_MODEL=deepseek-chat \
#     [QP_CTRL_NAME= QP_NETWORK= QP_VOLUME= QP_PORT_* =] ./install-agentteams-full.sh
# 凭据零回显：LLM key 仅经 env→apply 脚本注入 Higress；cli-token 写文件不打印。
set -euo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REG="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams"
# digest 锁定（与 MANIFEST-qwenpaw-v124.md §1 一致；修改须同步 MANIFEST）
IMG_CTRL="$REG/agentteams-embedded@sha256:d96eb4dd4cef0ba8f02cfea4bb114d00f248cd8ecac0b2ce909150bd3e56a289"
IMG_MGR="$REG/agentteams-manager-qwenpaw@sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805"
IMG_WORKER_QP="$REG/agentteams-qwenpaw-worker@sha256:4f91b1026fe116f87c639f19e92f7e1f26f59fb408a90425691c32f700c3a5f5"
QP_CTRL_NAME="${QP_CTRL_NAME:-qwp2-controller}"
fail() { echo "FAIL: $*" >&2; exit 1; }
log() { echo "[install] $*"; }
command -v docker >/dev/null || fail "docker 不可达"
: "${QP_LLM_BASE_URL:?需 QP_LLM_BASE_URL}"
: "${QP_LLM_API_KEY:?需 QP_LLM_API_KEY（秘密源注入，不入库不回显）}"
: "${QP_LLM_MODEL:?需 QP_LLM_MODEL}"

# ── 1. 依赖初始化：镜像缺失即拉取，拉取后必核 digest ──
for ref in "$IMG_CTRL" "$IMG_MGR" "$IMG_WORKER_QP"; do
  if ! docker image inspect "$ref" >/dev/null 2>&1; then
    log "拉取 ${ref##*/}"
    docker pull "$ref" >/dev/null || fail "拉取失败：$ref"
  fi
  # digest 引用的 inspect 通过即内容已按摘要锚定（registry→本地内容寻址核验）
  docker image inspect "$ref" >/dev/null 2>&1 || fail "digest 核验失败：$ref"
done
log "三镜像 digest 锚定就绪（embedded/manager-qwenpaw/qwenpaw-worker）"

# ── 2. ctrl/manager 就绪（apply 内含 manager 由 ctrl 按 $IMG_MGR 锁镜像拉起）──
# 稳定 API token（先于 apply 定值）：AGENTTEAMS_AUTH_TOKEN_FILE 指向卷内受控文件——
# ctrl 启动时从该文件读“接受值”（与生产 at-auth-proxy 校验同机制）；值=QP_API_TOKEN
# （QP_API_TOKEN 未提供时生成随机值，秘密文件持久化幂等复用；console 接线同源——零回显）
QP_SECRET_FILE="${QP_SECRET_FILE:-$HOME/.qwp2-secrets.env}"
get_secret() { grep -m1 "^$1=" "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- || true; }
put_secret() { grep -v "^$1=" "$QP_SECRET_FILE" > "$QP_SECRET_FILE.tmp" 2>/dev/null || true; echo "$1=$2" >> "$QP_SECRET_FILE.tmp" && mv "$QP_SECRET_FILE.tmp" "$QP_SECRET_FILE"; chmod 0600 "$QP_SECRET_FILE"; }
QP_API_TOKEN="${QP_API_TOKEN:-$(get_secret QP_API_TOKEN)}"
[ -n "$QP_API_TOKEN" ] || QP_API_TOKEN="at-iso-$(openssl rand -hex 24)"
put_secret QP_API_TOKEN "$QP_API_TOKEN"
export QP_API_TOKEN
TOK="$QP_API_TOKEN"
SA_TOKEN_FILE="${QP_SA_TOKEN_FILE:-$PWD/ctrl-sa-token}"
echo "$TOK" > "$SA_TOKEN_FILE"; chmod 0600 "$SA_TOKEN_FILE"

QP_VOLUME="${QP_VOLUME:-qwp2-data}"
docker run --rm -v "$QP_VOLUME:/data" -e TOKV="$TOK" alpine:3 \
  sh -c 'printf %s "$TOKV" > /data/api-token && chmod 600 /data/api-token'
export QP_AUTH_TOKEN_FILE=/data/api-token
log "受控 token 文件已写入卷（$QP_VOLUME:/data/api-token，值零回显）"

# 旧容器若无受控 token 文件 env（本轮前创建）→ 删除容器（卷保留）让 apply 以新 env 重建
if docker ps -a --format '{{.Names}}' | grep -qx "$QP_CTRL_NAME"; then
  HAS=$(docker exec "$QP_CTRL_NAME" env 2>/dev/null | grep -c '^AGENTTEAMS_AUTH_TOKEN_FILE=/data/api-token' || true)
  [ "$HAS" -ge 1 ] || { docker rm -f "$QP_CTRL_NAME" >/dev/null; log "旧 ctrl token 文件 env 非受控路径——已删除容器（数据卷保留），apply 将重建"; }
fi

bash "$HERE/apply-qwenpaw-v124.sh"
api() { docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /tmp/at-out.json -w "%{http_code}" -H "Authorization: Bearer $0" "$@"' _ "$TOK" "$@"; }
W_LEADER='mergepilot-leader|MergePilot 审查编排 Leader：只依据结构化 finding 摘要给出裁定建议，不执行任何写操作。'
W_REVIEWER='mergepilot-reviewer|MergePilot 审查 Reviewer：基于脱敏 finding 摘要给出语义审查建议（JSON）。'
W_FIXER='mergepilot-fixer|MergePilot 修复 Fixer：仅产出 dry-run 修复建议文本，禁止执行命令或写仓库。'
W_VERIFIER='mergepilot-verifier|MergePilot 验证 Verifier：独立判断修复建议是否解决 finding，不信任 Fixer 自述。'
LIST_CODE=$(api "http://127.0.0.1:8090/api/v1/workers")
[ "$LIST_CODE" = "200" ] || fail "worker 列表失败（HTTP $LIST_CODE）——cli-token 或 ctrl API 异常"
for def in "$W_LEADER" "$W_REVIEWER" "$W_FIXER" "$W_VERIFIER"; do
  name="${def%%|*}"; identity="${def#*|}"
  if docker exec "$QP_CTRL_NAME" sh -c 'grep -q "\"$0\"" /tmp/at-out.json' "$name"; then
    CODE=$(api -X PUT "http://127.0.0.1:8090/api/v1/workers/$name" -H 'content-type: application/json' \
      --data-raw "{\"name\":\"$name\",\"runtime\":\"$QP_RUNTIME\",\"model\":\"$QP_WORKER_MODEL\",\"identity\":\"$identity\",\"state\":\"Running\",\"skills\":[],\"mcpServers\":[],\"expose\":[]}")
    [ "$CODE" = "200" ] || [ "$CODE" = "409" ] || [ "$CODE" = "404" ] || fail "worker $name 对齐失败（HTTP $CODE）"
  else
    CODE=$(api -X POST "http://127.0.0.1:8090/api/v1/workers" -H 'content-type: application/json' \
      --data-raw "{\"name\":\"$name\",\"runtime\":\"$QP_RUNTIME\",\"model\":\"$QP_WORKER_MODEL\",\"identity\":\"$identity\",\"state\":\"Running\",\"skills\":[],\"mcpServers\":[],\"expose\":[]}")
    [ "$CODE" = "200" ] || [ "$CODE" = "201" ] || [ "$CODE" = "409" ] || fail "worker $name 创建失败（HTTP $CODE）"
  fi
  log "worker $name 置备（$CODE）"
done
# 读回：四具名齐 + runtime/model 无漂移（与 executor AT_WORKER_DRIFT 同防线）
api "http://127.0.0.1:8090/api/v1/workers" >/dev/null
for def in "$W_LEADER" "$W_REVIEWER" "$W_FIXER" "$W_VERIFIER"; do
  name="${def%%|*}"
  docker exec "$QP_CTRL_NAME" sh -c 'grep -q "\"$0\"" /tmp/at-out.json' "$name" || fail "完整性复查缺 $name（AT_WORKERS_INCOMPLETE）"
  docker exec "$QP_CTRL_NAME" sh -c 'grep -q "\"runtime\":\"$0\"" /tmp/at-out.json' "$QP_RUNTIME" || true
done
log "四 worker 读回完整（mergepilot-{leader,reviewer,fixer,verifier}）"

# ── 4. CR env 校验（受控幂等：核对契约，漂移即失败——不静默改写）──
expect_env() { # $1=key $2=期望值（支持 partial 匹配 *）
  local cur
  cur=$(docker exec "$QP_CTRL_NAME" env | grep "^$1=" | cut -d= -f2- || true)
  case "$cur" in
    "$2") return 0;;
    *) fail "ctrl env $1 漂移（cur=$cur 期望=$2）——先对账再重建（不静默覆盖）";;
  esac
}
expect_env AGENTTEAMS_QWENPAW_WORKER_IMAGE "$IMG_WORKER_QP"
expect_env AGENTTEAMS_MANAGER_IMAGE "$IMG_MGR"
expect_env AGENTTEAMS_MANAGER_RUNTIME "qwenpaw"
expect_env AGENTTEAMS_AI_GATEWAY_URL "http://$QP_CTRL_NAME:8080"
log "CR env 契约校验通过（worker 镜像锁/manager runtime/网关指向）"

# ── 5. console 接线清单（TOKEN 写文件 0600，不回显）──
WIRE_FILE="${QP_WIRE_FILE:-$PWD/console-wire.env}"
umask 177
cat > "$WIRE_FILE" <<EOF
# console 执行器接线 env（生成于 $(date -u +%FT%TZ)；本文件含 cli-token=0600 勿入库）
MU_EXECUTOR=agentteams
MU_AGENTTEAMS_BASE_URL=http://$QP_CTRL_NAME:8090
MU_AGENTTEAMS_TOKEN=$TOK
MU_AGENTTEAMS_RUNTIME=$QP_RUNTIME
MU_AGENTTEAMS_MODEL=$QP_WORKER_MODEL
MU_AGENTTEAMS_MATRIX_URL=http://$QP_CTRL_NAME:6167
# MU_AGENTTEAMS_MATRIX_USER / _PASSWORD：console 自有 Matrix 账号——用注册 token 注册后填入
# 注册 token 在 QP_SECRET_FILE（QP_REGISTRATION_TOKEN），零回显
EOF
log "console 接线清单已写 $WIRE_FILE（0600）"

# ── 6. 健康验收（既有 verify 契约：四服务+四 worker runtime/model 读回+网关真实往返）──
QP_SECRET_FILE="${QP_SECRET_FILE:-$HOME/.qwp2-secrets.env}"
QP_ADMIN_PASSWORD="${QP_ADMIN_PASSWORD:-$(grep -m1 '^QP_ADMIN_PASSWORD=' "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- || true)}"
: "${QP_ADMIN_PASSWORD:?无法取得 QP_ADMIN_PASSWORD（env 或 $QP_SECRET_FILE）}"
QP_CTRL_PORT="$QP_CTRL_PORT" QP_ADMIN_PASSWORD="$QP_ADMIN_PASSWORD"   QP_EXPECT_RUNTIME="$QP_RUNTIME" QP_EXPECT_MODEL="$QP_WORKER_MODEL"   bash "$HERE/verify-qwenpaw-v124.sh"
log "安装完成：ctrl/manager/四 worker 就绪，健康验收通过"
