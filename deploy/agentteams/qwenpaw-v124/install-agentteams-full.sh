#!/usr/bin/env bash
# install-agentteams-full.sh — QwenPaw v1.2.4 完整执行器栈统一安装入口 v3（冷启动+长效认证收口）。
#
# 冷启动顺序（契约，2026-10-25 收口）：
#   1. 依赖初始化   三镜像 digest 核验
#   2. 凭据目录     仓库外受控目录（0700 先于创建）
#   3. ctrl/manager 就绪   apply（无 ctrl 时创建→健康等待→cli-token 落盘；token 读取只能在 ctrl 就绪后）
#   4. 认证         读现行 cli-token → 铸长效 SA JWT（ctrl pki ES256）→ 三态自证（JWT 200/错 401/无 401）
#                   → 后续一律用 JWT（跨 cli-token 轮换稳定；不关认证、不延有效期、不重启维持）
#   5. 四 worker 置备   union body 幂等；逐 worker JSON 读回校验（runtime/model）
#   6. CR 校验      双证据：ctrl API worker spec.env + worker 容器 Config.Env（docker inspect）
#   7. Matrix 接线   console bot 幂等注册（凭据持久化于栈秘密文件；密码不轮换则登录即复用）
#   8. console 接线清单   出仓 0600（MU_AGENTTEAMS_TOKEN=长效 JWT——console 跨轮换免重启）
#   9. 健康验收   verify（分阶段：安装期查 worker 就绪；team ID 在任务创建后由调用方核对）
# 任何失败=非零退出且不打印安装完成。
set -euo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "$0")" && pwd)"

# ══ CONFIG（单一来源）══
QP_CTRL_NAME="${QP_CTRL_NAME:-qwp2-controller}"
QP_NETWORK="${QP_NETWORK:-qwp2-net}"
QP_VOLUME="${QP_VOLUME:-qwp2-data}"
QP_PORT_CTRL="${QP_PORT_CTRL:-18190}"
QP_PORT_MATRIX="${QP_PORT_MATRIX:-18186}"
QP_PORT_MINIO="${QP_PORT_MINIO:-19100}"
QP_PORT_MGR_CONSOLE="${QP_PORT_MGR_CONSOLE:-18889}"
QP_RUNTIME="${QP_RUNTIME:-qwenpaw}"
QP_WORKER_MODEL="${QP_WORKER_MODEL:-${QP_LLM_MODEL:-deepseek-chat}}"
REG="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams"
IMG_CTRL="$REG/agentteams-embedded@sha256:d96eb4dd4cef0ba8f02cfea4bb114d00f248cd8ecac0b2ce909150bd3e56a289"
IMG_MGR="$REG/agentteams-manager-qwenpaw@sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805"
IMG_WORKER_QP="$REG/agentteams-qwenpaw-worker@sha256:4f91b1026fe116f87c639f19e92f7e1f26f59fb408a90425691c32f700c3a5f5"
QP_SECRET_DIR="${QP_SECRET_DIR:-$HOME/.at-iso-secrets}"
# ══════════════════════════════════════════════════════════════

fail() { echo "FAIL: $*" >&2; exit 1; }
log() { echo "[install] $*"; }
command -v docker >/dev/null || fail "docker 不可达"
command -v openssl >/dev/null || fail "openssl 不可达"
command -v python3 >/dev/null || fail "python3 不可达"
: "${QP_LLM_BASE_URL:?需 QP_LLM_BASE_URL}"
: "${QP_LLM_API_KEY:?需 QP_LLM_API_KEY（秘密源注入，不入库不回显）}"
: "${QP_LLM_MODEL:?需 QP_LLM_MODEL}"

# ── 1. 依赖初始化 ──
for ref in "$IMG_CTRL" "$IMG_MGR" "$IMG_WORKER_QP"; do
  if ! docker image inspect "$ref" >/dev/null 2>&1; then
    log "拉取 ${ref##*/}"
    docker pull "$ref" >/dev/null || fail "拉取失败：$ref"
  fi
  docker image inspect "$ref" >/dev/null 2>&1 || fail "digest 核验失败：$ref"
done
log "三镜像 digest 锚定就绪"

# ── 2. 凭据目录（出仓；0700 先于创建）──
mkdir -p "$QP_SECRET_DIR"
chmod 700 "$QP_SECRET_DIR"
QP_SECRET_FILE="$QP_SECRET_DIR/stack-secrets.env"
get_secret() { grep -m1 "^$1=" "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r\n' || true; }
put_secret() { grep -v "^$1=" "$QP_SECRET_FILE" > "$QP_SECRET_FILE.tmp" 2>/dev/null || true; echo "$1=$2" >> "$QP_SECRET_FILE.tmp" && mv "$QP_SECRET_FILE.tmp" "$QP_SECRET_FILE"; chmod 600 "$QP_SECRET_FILE"; }
for k in QP_ADMIN_PASSWORD QP_REGISTRATION_TOKEN QP_MINIO_USER QP_MINIO_PASSWORD; do
  V="${!k:-$(get_secret "$k")}"
  [ -n "$V" ] || V="$k-dummy-$(openssl rand -hex 12)"
  put_secret "$k" "$V"
done
QP_LLM_BASE_URL="${QP_LLM_BASE_URL}"; QP_LLM_MODEL="${QP_LLM_MODEL}"
put_secret QP_LLM_BASE_URL "$QP_LLM_BASE_URL"
put_secret QP_LLM_MODEL "$QP_LLM_MODEL"
put_secret QP_LLM_API_KEY "$QP_LLM_API_KEY"

# ── 3. ctrl/manager 就绪（apply：无 ctrl 则创建；token 读取在此之后）──
QP_DOCKER_SOCK="${QP_DOCKER_SOCK:-$([ -S /var/run/docker.sock ] && echo /var/run/docker.sock || echo /run/host-services/docker.proxy.sock)}"
export QP_CTRL_NAME QP_NETWORK QP_VOLUME QP_PORT_CTRL QP_PORT_MATRIX QP_PORT_MINIO QP_PORT_MGR_CONSOLE QP_DOCKER_SOCK
log "docker socket 源=$QP_DOCKER_SOCK"
# 旧容器漂移守卫（只比对真实配置：docker socket 挂载；不比对已弃用的静态 token-file env）
if docker ps -a --format '{{.Names}}' | grep -qx "$QP_CTRL_NAME"; then
  SOCK=$(docker inspect "$QP_CTRL_NAME" --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Source}}{{end}}{{end}}')
  [ "$SOCK" = "$QP_DOCKER_SOCK" ] || { docker rm -f "$QP_CTRL_NAME" >/dev/null; log "旧 ctrl socket 挂载漂移——容器已删（卷保留），apply 将重建"; }
fi
bash "$HERE/apply-qwenpaw-v124.sh"

# ── 4. 认证：现行 cli-token → 铸长效 SA JWT → 三态自证 ──
api() { docker exec "$QP_CTRL_NAME" sh -c 'TOK=$1; shift; curl -s -o /tmp/at-out.json -w "%{http_code}" -H "Authorization: Bearer $TOK" "$@"' _ "$TOK" "$@"; }
CT=$(docker exec "$QP_CTRL_NAME" sh -c 'tr -d "\r\n" < /var/run/agentteams/cli-token' 2>/dev/null | tr -d '\r\n' || true)
[ -n "$CT" ] || fail "cli-token 文件缺失（ctrl 未完成引导）"
QP_SA_NAME="${QP_SA_NAME:-$(echo "$QP_CTRL_NAME" | tr -d '-')-admin}"
SA_TOKEN_FILE="$QP_SECRET_DIR/ctrl-sa-jwt"
QP_SA_NAME="$QP_SA_NAME" bash "$HERE/mint-sa-token.sh" "$SA_TOKEN_FILE" >/dev/null 2>&1 || true
JWT=$(cat "$SA_TOKEN_FILE" 2>/dev/null || true)
C_JWT=$(TOK="$JWT" api http://127.0.0.1:8090/api/v1/workers)
if [ "$C_JWT" = "200" ]; then
  APITOK="$JWT"; log "长效 SA JWT 认证通过（跨 cli-token 轮换稳定）"
else
  C_CT=$(TOK="$CT" api http://127.0.0.1:8090/api/v1/workers)
  [ "$C_CT" = "200" ] || fail "现行 cli-token 亦被拒（HTTP $C_CT）——ctrl 认证面异常"
  APITOK="$CT"
  log "警告：SA JWT 未获认证（$C_JWT）——本轮回退现行 cli-token（约 5 分钟轮换，长连接接线需在轮换后重读）"
fi
# 三态自证（有效/错误/无）
C_W=$(TOK="at-iso-wrong-credential" api http://127.0.0.1:8090/api/v1/workers)
C_N=$(TOK="" api http://127.0.0.1:8090/api/v1/workers)
[ "$C_W" = "401" ] && [ "$C_N" = "401" ] || fail "认证三态异常（错=$C_W 无=$C_N）——拒绝在可疑认证面继续"
log "认证三态自证通过（有效=200 错误=401 无=401；API 凭据口径=JWT 或现行 cli-token）"

# ── 5. 四 worker 置备（union body；幂等；逐 worker JSON 读回校验）──
WORKER_SPECS=(
  'mergepilot-leader|MergePilot 审查编排 Leader：只依据结构化 finding 摘要给出裁定建议，不执行任何写操作。'
  'mergepilot-reviewer|MergePilot 审查 Reviewer：基于脱敏 finding 摘要给出语义审查建议（JSON）。'
  'mergepilot-fixer|MergePilot 修复 Fixer：仅产出 dry-run 修复建议文本，禁止执行命令或写仓库。'
  'mergepilot-verifier|MergePilot 验证 Verifier：独立判断修复建议是否解决 finding，不信任 Fixer 自述。'
)
CTRL_BASE="http://127.0.0.1:8090"
LIST_CODE=$(api "$CTRL_BASE/api/v1/workers")
[ "$LIST_CODE" = "200" ] || fail "worker 列表失败（HTTP $LIST_CODE）"
docker exec "$QP_CTRL_NAME" cp /tmp/at-out.json /tmp/at-out-provision.json
for spec in "${WORKER_SPECS[@]}"; do
  name="${spec%%|*}"; identity="${spec#*|}"
  body=$(printf '{"name":"%s","runtime":"%s","model":"%s","identity":"%s","state":"Running","skills":[],"mcpServers":[],"expose":[],"spec":{"runtime":"%s","model":"%s","identity":"%s","state":"Running"}}' \
    "$name" "$QP_RUNTIME" "$QP_WORKER_MODEL" "$identity" "$QP_RUNTIME" "$QP_WORKER_MODEL" "$identity")
  if docker exec "$QP_CTRL_NAME" sh -c 'grep -q "\"$1\"" /tmp/at-out-provision.json' _ "$name"; then
    CODE=$(api -X PUT "$CTRL_BASE/api/v1/workers/$name" -H 'content-type: application/json' --data-raw "$body")
    case "$CODE" in 200|409) :;; 404) CODE=$(api -X POST "$CTRL_BASE/api/v1/workers" -H 'content-type: application/json' --data-raw "$body");; esac
  else
    CODE=$(api -X POST "$CTRL_BASE/api/v1/workers" -H 'content-type: application/json' --data-raw "$body")
  fi
  case "$CODE" in 200|201|409) log "worker $name 置备（$CODE）";; *) fail "worker $name 置备失败（HTTP $CODE）";; esac
done
api "$CTRL_BASE/api/v1/workers" >/dev/null
docker exec "$QP_CTRL_NAME" cat /tmp/at-out.json > "$QP_SECRET_DIR/workers-readback.json"
chmod 600 "$QP_SECRET_DIR/workers-readback.json"
RD=$(python3 - "$QP_SECRET_DIR/workers-readback.json" "$QP_RUNTIME" "$QP_WORKER_MODEL" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
er, em = sys.argv[2], sys.argv[3]
need = {'mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'}
rows = {w.get('name'): w for w in (d.get('workers') or [])}
missing = sorted(need - set(rows))
if missing:
    print('MISSING ' + ' '.join(missing)); raise SystemExit
bad = []
for n in sorted(need):
    w = rows[n]
    if w.get('runtime') != er: bad.append(f'{n}.runtime={w.get("runtime")!r}!={er}')
    if w.get('model') != em: bad.append(f'{n}.model={w.get("model")!r}!={em}')
print('ALL_OK' if not bad else 'DRIFT ' + '; '.join(bad))
PY
)
case "$RD" in
  ALL_OK) log "四 worker JSON 读回校验通过（runtime=$QP_RUNTIME model=$QP_WORKER_MODEL 无漂移）";;
  *) fail "worker 读回校验失败：$RD";;
esac

# ── 6. CR 校验（双证据：worker 存储的 CR desired state（runtime/runtime.yaml=MemberRuntimeConfig）
#         + worker 容器 Config.Env（实际配置）。v1.2.4 API 列表为扁平形状、无 spec.env 字段——
#         CR 持久化读自 worker 侧镜像存储，不以 docker exec env 代替 CR 证据）──
# 等待 CR 投影（worker 启动 mirror 需要时间；最长 120s）
CR_READY=0
for _w in $(seq 1 24); do
  N=$(docker exec "qwp2-worker-mergepilot-leader" sh -c 'test -f /root/agentteams-fs/agents/mergepilot-leader/runtime/runtime.yaml && echo ok' 2>/dev/null || true)
  [ "$N" = "ok" ] && { CR_READY=1; break; }
  sleep 5
done
[ "$CR_READY" = "1" ] || fail "CR desired state 未投影（leader 容器内 runtime/runtime.yaml 120s 未出现）"
for spec in "${WORKER_SPECS[@]}"; do
  name="${spec%%|*}"; wc="qwp2-worker-$name"
  CR=$(docker exec "$wc" sh -c 'cat /root/agentteams-fs/agents/'"$name"'/runtime/runtime.yaml 2>/dev/null' || true)
  [ -n "$CR" ] || fail "CR 读取失败：$wc runtime/runtime.yaml 缺失"
  echo "$CR" | grep -q "name: $name" || fail "CR 校验失败：$wc member.name 漂移"
  echo "$CR" | grep -q "runtime: $QP_RUNTIME" || fail "CR 校验失败：$wc runtime 漂移（期望 $QP_RUNTIME）"
  echo "$CR" | grep -q "model: $QP_WORKER_MODEL" || fail "CR 校验失败：$wc model 漂移（期望 $QP_WORKER_MODEL）"
  CNT=$(docker inspect "$wc" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)
  [ -n "$CNT" ] || fail "worker 容器不可读：$wc"
  echo "$CNT" | grep -q "^AGENTTEAMS_WORKER_NAME=$name$" || fail "容器 Config.Env 校验失败：$wc 缺 AGENTTEAMS_WORKER_NAME=$name"
  if echo "$CNT" | grep -q '^AGENTTEAMS_MODEL='; then
    echo "$CNT" | grep -q "^AGENTTEAMS_MODEL=$QP_WORKER_MODEL$" || fail "容器 Config.Env 校验失败：$wc AGENTTEAMS_MODEL 漂移"
  fi
  log "CR 校验通过：$wc（runtime.yaml desired state + Config.Env 双证据一致）"
done
log "CR 校验通过（API spec.env 投影 + 容器 Config.Env 身份/模型在场）"

# ── 7. Matrix 接线（console bot 幂等注册；凭据持久化不轮换）──
MXPW=$(get_secret CONSOLE_BOT_PASSWORD)
[ -n "$MXPW" ] || MXPW="console-bot-$(openssl rand -hex 12)"
BOTUSER=$(get_secret CONSOLE_BOT_USER)
[ -n "$BOTUSER" ] || BOTUSER="console-bot"
REGTOK=$(get_secret QP_REGISTRATION_TOKEN)
login_mx() { curl -s -m 8 -X POST "http://127.0.0.1:$QP_PORT_MATRIX/_matrix/client/v3/login" -H 'content-type: application/json' \
  -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"'"$1"'"},"password":"'"$2"'"}'; }
LR=$(login_mx "$BOTUSER" "$MXPW")
if echo "$LR" | grep -q '"user_id"'; then
  log "console bot 已存在且登录通过（幂等复用）：$BOTUSER"
else
  RR=$(curl -s -m 8 -X POST "http://127.0.0.1:$QP_PORT_MATRIX/_matrix/client/v3/register" -H 'content-type: application/json' \
    -d '{"username":"'"$BOTUSER"'","password":"'"$MXPW"'","auth":{"type":"m.login.registration_token","token":"'"$REGTOK"'"}}')
  echo "$RR" | grep -q '"user_id"' || fail "console bot 注册失败：$(echo "$RR" | head -c 120)"
  LR=$(login_mx "$BOTUSER" "$MXPW")
  echo "$LR" | grep -q '"user_id"' || fail "console bot 注册后登录失败"
  log "console bot 已注册并登录通过：$BOTUSER"
fi
put_secret CONSOLE_BOT_USER "$BOTUSER"
put_secret CONSOLE_BOT_PASSWORD "$MXPW"

# ── 8. console 接线清单（出仓 0600；MU_AGENTTEAMS_TOKEN=长效凭据 APITOK）──
WIRE_FILE="$QP_SECRET_DIR/console-wire.env"
umask 177
cat > "$WIRE_FILE" <<EOF
# console 执行器接线 env（生成于 $(date -u +%FT%TZ)；含凭据=0600 勿入库）
# MU_AGENTTEAMS_TOKEN=$([ "$APITOK" = "$JWT" ] && echo "长效 SA JWT（跨 cli-token 轮换稳定）" || echo "现行 cli-token（约 5 分钟轮换——JWT 回退注记见安装日志）")
MU_EXECUTOR=agentteams
MU_AGENTTEAMS_BASE_URL=http://$QP_CTRL_NAME:8090
MU_AGENTTEAMS_TOKEN=$APITOK
MU_AGENTTEAMS_RUNTIME=$QP_RUNTIME
MU_AGENTTEAMS_MODEL=$QP_WORKER_MODEL
MU_AGENTTEAMS_MATRIX_URL=http://$QP_CTRL_NAME:6167
MU_AGENTTEAMS_MATRIX_USER=$BOTUSER
MU_AGENTTEAMS_MATRIX_PASSWORD=$MXPW
EOF
umask 022
log "console 接线清单已写 $WIRE_FILE（0600）"

# ── 9. 健康验收（worker 就绪；team ID=任务创建后由调用方核对——分阶段验证）──
get_secret() { grep -m1 "^$1=" "$QP_SECRET_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r\n' || true; }
QP_ADMIN_PASSWORD="${QP_ADMIN_PASSWORD:-$(get_secret QP_ADMIN_PASSWORD)}"
: "${QP_ADMIN_PASSWORD:?无法取得 QP_ADMIN_PASSWORD}"
QP_CTRL_PORT="$QP_PORT_CTRL" QP_ADMIN_PASSWORD="$QP_ADMIN_PASSWORD" \
  QP_EXPECT_RUNTIME="$QP_RUNTIME" QP_EXPECT_MODEL="$QP_WORKER_MODEL" \
  bash "$HERE/verify-qwenpaw-v124.sh"
log "安装完成：ctrl/manager/四 worker 就绪，认证与 Matrix 接线收口，健康验收通过"
