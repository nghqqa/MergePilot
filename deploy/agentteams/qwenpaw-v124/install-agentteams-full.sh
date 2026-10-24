#!/usr/bin/env bash
# install-agentteams-full.sh — QwenPaw v1.2.4 完整执行器栈统一安装入口。
#
# 流水线（顺序即契约；幂等可重跑，不覆盖已有身份/配置；任何失败=非零退出且不打印安装完成）：
#   1. 依赖初始化   三镜像 digest 核验（缺失即拉取；拉取后必核验）
#   2. 凭据         稳定 API token（AGENTTEAMS_AUTH_TOKEN_FILE 卷内受控文件；值/文件均出仓）
#   3. ctrl/manager 就绪   调 apply-qwenpaw-v124.sh（controller 健康+Higress 播种+网关探测）
#   4. 四 worker 置备   经 ctrl API（union body；幂等=列表→缺者建/在者对齐→逐 worker JSON 读回校验）
#   5. CR env 校验   实读 worker 容器 spec.env（非 ctrl env 代替）
#   6. console 接线清单   写出仓受控目录（0600，token 零回显）
#   7. 健康验收   verify-qwenpaw-v124.sh
#
# 用法：QP_LLM_BASE_URL/QP_LLM_API_KEY/QP_LLM_MODEL 必填；其余配置见下方 CONFIG 块。
# 请求助手参数契约：token 在 $1，消费后 shift——curl 只收到 URL/参数
# （2026-10-24 事故：旧写法 token 放 $0、URL 放 $@ → Authorization 恒空 = 六类 bearer 全 401 真因）。
set -euo pipefail
export MSYS_NO_PATHCONV=1

HERE="$(cd "$(dirname "$0")" && pwd)"

# ══ CONFIG（单一来源：runtime/model/端口/资源命名全部只在此定义 ══
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
# 凭据/接线产物：仓库外受控目录（权限先于文件创建；绝不写入 repo 工作区）
QP_SECRET_DIR="${QP_SECRET_DIR:-$HOME/.at-iso-secrets}"
SA_TOKEN_FILE="$QP_SECRET_DIR/ctrl-api-token"
WIRE_FILE="$QP_SECRET_DIR/console-wire.env"
# ══════════════════════════════════════════════════════════════

fail() { echo "FAIL: $*" >&2; exit 1; }
log() { echo "[install] $*"; }
command -v docker >/dev/null || fail "docker 不可达"
command -v openssl >/dev/null || fail "openssl 不可达"
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
log "三镜像 digest 锚定就绪（embedded/manager-qwenpaw/qwenpaw-worker）"

# ── 2. 凭据（cli-token 即读即用）──
# 实证（#520 续波）：ctrl 只认自身轮换签发的 cli-token（静态值/自铸 JWT/AGENTTEAMS_AUTH_TOKEN*
# env 均不生效——曾误当凭据注入位，已在 BLOCKED 文档勘误）。cli-token 约 5 分钟轮换：
# 读→试→非 200 重读（安装期窗口足够；console 长连接接线见接线清单内的轮换注记）。
QP_SECRET_DIR="${QP_SECRET_DIR:-$HOME/.at-iso-secrets}"
mkdir -p "$QP_SECRET_DIR"; chmod 700 "$QP_SECRET_DIR"
SA_TOKEN_FILE="$QP_SECRET_DIR/ctrl-api-token"
TOK=""
for _try in $(seq 1 30); do
  T=$(docker exec "$QP_CTRL_NAME" sh -c 'tr -d "
" < /var/run/agentteams/cli-token' 2>/dev/null | tr -d "
" || true)
  if [ -n "$T" ]; then
    C=$(docker exec "$QP_CTRL_NAME" sh -c 'T=$1; shift; curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $T" http://127.0.0.1:8090/api/v1/workers' _ "$T")
    if [ "$C" = "200" ]; then TOK="$T"; printf '%s' "$TOK" > "$SA_TOKEN_FILE"; chmod 600 "$SA_TOKEN_FILE"; break; fi
  fi
  sleep 3
done
[ -n "$TOK" ] || fail "90s 内未取得可用 cli-token（ctrl 未就绪或轮换异常）"
log "cli-token 即读即用（$SA_TOKEN_FILE，0600；轮换期约 5 分钟——长连接接线见清单注记）"

# ── 3. ctrl/manager 就绪（apply 读 CONFIG 同源值）──
export QP_CTRL_NAME QP_NETWORK QP_VOLUME QP_PORT_CTRL QP_PORT_MATRIX QP_PORT_MINIO QP_PORT_MGR_CONSOLE
# docker socket 源：Docker Desktop=/run/host-services/docker.proxy.sock；原生 dockerd（WSL/Linux）=/var/run/docker.sock
QP_DOCKER_SOCK="${QP_DOCKER_SOCK:-$([ -S /var/run/docker.sock ] && echo /var/run/docker.sock || echo /run/host-services/docker.proxy.sock)}"
export QP_DOCKER_SOCK
log "docker socket 源=$QP_DOCKER_SOCK"
# 旧容器漂移守卫：token 文件 env 或 docker socket 挂载与期望不符 → 删容器（卷保留）让 apply 重建
if docker ps -a --format '{{.Names}}' | grep -qx "$QP_CTRL_NAME"; then
  HAS=$(docker exec "$QP_CTRL_NAME" env 2>/dev/null | grep -c '^AGENTTEAMS_AUTH_TOKEN_FILE=/data/api-token' || true)
  SOCK=$(docker inspect "$QP_CTRL_NAME" --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Source}}{{end}}{{end}}')
  if [ "$HAS" -lt 1 ] || [ "$SOCK" != "$QP_DOCKER_SOCK" ]; then
    docker rm -f "$QP_CTRL_NAME" >/dev/null
    log "旧 ctrl 配置漂移（token env 或 socket 挂载）——容器已删（卷保留），apply 将重建"
  fi
fi
bash "$HERE/apply-qwenpaw-v124.sh"

# 请求助手（$1=token 消费后 shift；curl 只收 URL/参数）
api() { docker exec "$QP_CTRL_NAME" sh -c 'TOK=$1; shift; curl -s -o /tmp/at-out.json -w "%{http_code}" -H "Authorization: Bearer $TOK" "$@"' _ "$TOK" "$@"; }

# ── 4. 四 worker 置备（union body；幂等；逐 worker JSON 读回校验）──
# 定义源=console/backend/lib/multiuser/agents/agentteams-executor.mjs AGENTTEAMS_WORKERS（锁步）
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
# 读回校验：逐 worker JSON 解析 runtime/model（宿主 python；漂移=失败，无吞错）
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

# ── 5. CR env 校验（实读 worker 容器 spec.env——非 ctrl env 代替）──
# worker 容器由 ctrl 经 docker sock 创建（与 ctrl 共 netns，名=qwp2-worker-<name>）
for spec in "${WORKER_SPECS[@]}"; do
  name="${spec%%|*}"; wc="qwp2-worker-$name"
  if docker ps --format '{{.Names}}' | grep -qx "$wc"; then
    WENV=$(docker exec "$wc" env 2>/dev/null || docker inspect "$wc" --format '{{range .Config.Env}}{{println .}}{{end}}')
    echo "$WENV" | grep -q "^AGENTTEAMS_WORKER_NAME=$name$" || fail "CR spec.env 校验失败：$wc 缺 AGENTTEAMS_WORKER_NAME=$name"
  else
    fail "worker 容器不存在：$wc（置备后应由 ctrl reconcile 拉起——检查 ctrl 日志）"
  fi
done
log "CR spec.env 实读校验通过（四 worker 容器身份在场）"

# ── 6. console 接线清单（出仓受控目录，0600，token 零回显）──
umask 177
cat > "$WIRE_FILE" <<EOF
# console 执行器接线 env（生成于 $(date -u +%FT%TZ)；含稳定 API token=0600 勿入库）
MU_EXECUTOR=agentteams
MU_AGENTTEAMS_BASE_URL=http://$QP_CTRL_NAME:8090
MU_AGENTTEAMS_TOKEN=$TOK
# ↑ cli-token 约 5 分钟轮换：console 长驻形态须走 at-auth-proxy（生产模式，长效 JWT）
#   或在 401 时以新 cli-token 重启 console（隔离测试可接受）。MU_AGENTTEAMS_RUNTIME=$QP_RUNTIME
MU_AGENTTEAMS_MODEL=$QP_WORKER_MODEL
MU_AGENTTEAMS_MATRIX_URL=http://$QP_CTRL_NAME:6167
# MU_AGENTTEAMS_MATRIX_USER/_PASSWORD：console 自有 Matrix 账号——用注册 token 注册后填入
# 注册 token 在 $QP_SECRET_DIR/stack-secrets.env（QP_REGISTRATION_TOKEN）
EOF
umask 022
log "console 接线清单已写 $WIRE_FILE（0600）"

# ── 7. 健康验收（verify 失败=本入口失败；不打印安装完成）──
get_secret() { f=$2; grep -m1 "^$1=" "$f" 2>/dev/null | cut -d= -f2- || true; }
QP_ADMIN_PASSWORD="${QP_ADMIN_PASSWORD:-$(get_secret QP_ADMIN_PASSWORD /home/ngh/.qwp2-secrets.env)}"
: "${QP_ADMIN_PASSWORD:?无法取得 QP_ADMIN_PASSWORD}"
QP_CTRL_PORT="$QP_PORT_CTRL" QP_ADMIN_PASSWORD="$QP_ADMIN_PASSWORD" \
  QP_EXPECT_RUNTIME="$QP_RUNTIME" QP_EXPECT_MODEL="$QP_WORKER_MODEL" \
  bash "$HERE/verify-qwenpaw-v124.sh"
log "安装完成：ctrl/manager/四 worker 就绪，健康验收通过"
