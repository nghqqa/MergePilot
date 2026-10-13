#!/usr/bin/env bash
# update-ctrl-env.sh — AgentTeams 生产 ctrl 容器 env 的可重复更新/受控重建工具
#
# 背景（2026-10-13 波定案）：
#   * Worker CR 的 spec.env 变更【不进入】控制器 specHash —— 存量容器不会被自动重建；
#     env 变更只在容器（重）创建时生效（唤醒路径按 CR 现值创建，或删除陈旧容器后由控制器重建）。
#   * ctrl 自身的 env 变更（AppService 开关/LLM key/worker 镜像 pin）只能通过容器重建生效；
#     手工 docker run 易漂移 —— 本工具把"捕获现值→注入标准增量→受控重建→健康检查→失败回滚"固化。
#
# 用法：
#   update-ctrl-env.sh                 # dry-run：打印计划（秘密值打码），不改任何东西
#   update-ctrl-env.sh --apply         # 执行受控重建
# 选项：
#   --ctrl NAME              目标 ctrl 容器（默认 agentteams-beta-ctrl）
#   --network NAME           网络名（默认从现容器读）
#   --llm-key-stdin          从 stdin 读现役 LLM key 写入 AGENTTEAMS_OPENAI_API_KEY
#                            （先对 api.deepseek.com 真实调用验证；401 拒绝执行）
#   --worker-image REF       更新 AGENTTEAMS_QWENPAW_WORKER_IMAGE pin
#   --no-appservice          不启用 Matrix AppService（默认启用）
#   --keep-old               重建成功后保留旧容器（默认：成功后删除旧容器，失败自动回滚）
#
# 幂等与锚：
#   * AppService AS/HS token 首次生成后持久化到 <DATA>/appservice-tokens.json（权限 600），
#     重复执行复用同一值 —— tuwunel 注册不漂移。
#   * 旧容器按 update-ctrl-prev-<ts> 命名保留（--keep-old）或删除（默认）；inspect 全量快照
#     落 <EV>/ctrl-inspect-<ts>.json 作为回滚锚。
#   * 失败（healthz 不通/容器退出）→ 自动用旧 env 重建旧容器名并停止，退出码 1。
#
# 纪律：秘密值不回显、不进日志；数据卷/状态 bind/端口/网络别名全部从现容器原样继承。
set -euo pipefail

CTRL="agentteams-beta-ctrl"
APPLY=0
LLM_KEY_STDIN=0
WORKER_IMAGE=""
NO_APPSERVICE=0
KEEP_OLD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;;
    --ctrl) CTRL="$2"; shift;;
    --network) NET_OVERRIDE="$2"; shift;;
    --llm-key-stdin) LLM_KEY_STDIN=1;;
    --worker-image) WORKER_IMAGE="$2"; shift;;
    --no-appservice) NO_APPSERVICE=1;;
    --keep-old) KEEP_OLD=1;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
  shift
done

EV="${EV:-$(pwd)/evidence-update-ctrl}"
mkdir -p "$EV"
TS=$(date -u +%Y%m%d-%H%M%S)

docker inspect "$CTRL" > /dev/null 2>&1 || { echo "FATAL: $CTRL 不存在" >&2; exit 1; }
docker inspect "$CTRL" > "$EV/ctrl-inspect-$TS.json"

MASK='s/(TOKEN|PASSWORD|SECRET|KEY|api_key)=.*/\1=<redacted>/'
echo "==> 捕获现值（env 值打码展示，完整值仅进临时 env 文件）"
docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -E "$MASK" | sed 's/^/    /'

NET="${NET_OVERRIDE:-$(docker inspect "$CTRL" --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}}{{end}}')}"
ALIASES=$(docker inspect "$CTRL" --format '{{json .NetworkSettings.Networks}}' | python -c '
import sys, json
nets = json.load(sys.stdin)
out = []
for v in nets.values():
    for a in (v.get("Aliases") or []):
        if a not in out:
            out.append(a)
sys.stdout.write(" ".join(out))
')
IMAGE=$(docker inspect "$CTRL" --format '{{.Config.Image}}')
PORTS=$(docker inspect "$CTRL" --format '{{range $p, $b := .HostConfig.PortBindings}}-p {{range $b}}{{.HostIp}}:{{.HostPort}}:{{$p}} {{end}}{{end}}')
BINDS=$(docker inspect "$CTRL" --format '{{range .HostConfig.Binds}}-v {{.}} {{end}}')
RESTART=$(docker inspect "$CTRL" --format '{{.HostConfig.RestartPolicy.Name}}')

echo "==> 计划：image=$IMAGE network=$NET aliases=$ALIASES restart=$RESTART"
echo "    端口/挂载：原样继承（见 $EV/ctrl-inspect-$TS.json）"

ENVF="$(cygpath -m "${TEMP:-/tmp}" 2>/dev/null || echo "${TEMP:-/tmp}")/atb-ctrl-env-$$.txt"
trap 'rm -f "$ENVF"' EXIT
docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$ENVF"

# ---- 标准增量 1：Matrix AppService（唤醒链） ----
if [ "$NO_APPSERVICE" = "0" ]; then
  DATAVOL=$(docker inspect "$CTRL" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
  TOKJSON="${TEMP:-/tmp}/atb-appservice-tokens.json"
  TOKJSON=$(cygpath -m "$TOKJSON" 2>/dev/null || echo "$TOKJSON")
  if MSYS_NO_PATHCONV=1 docker run --rm -v "$DATAVOL:/data" alpine test -f /data/appservice-tokens.json 2>/dev/null; then
    MSYS_NO_PATHCONV=1 docker run --rm -v "$DATAVOL:/data" alpine cat /data/appservice-tokens.json > "$TOKJSON"
    echo "==> AppService tokens：复用已持久化值"
  elif docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^AGENTTEAMS_MATRIX_APPSERVICE_AS_TOKEN='; then
    # 优先沿用现容器 env 里的 token（避免 tuwunel 重注册），并回填持久化
    docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' |       grep -E '^AGENTTEAMS_MATRIX_APPSERVICE_(AS|HS)_TOKEN=' |       python -c '
import sys, json
toks = {}
for l in sys.stdin:
    k, _, v = l.strip().partition("=")
    toks["as_token" if "AS_TOKEN" in k else "hs_token"] = v
sys.stdout.write(json.dumps(toks))
' > "$TOKJSON"
    docker run --rm -v "$DATAVOL:/data" -v "$TOKJSON:/tokens.json:ro" alpine       sh -c 'cp /tokens.json /data/appservice-tokens.json && chmod 600 /data/appservice-tokens.json'
    echo "==> AppService tokens：沿用现容器值并回填持久化"
  else
    python - <<'P1' > "$TOKJSON"
import secrets, json, sys
sys.stdout.write(json.dumps({
  "as_token": secrets.token_hex(32),
  "hs_token": secrets.token_hex(32),
}))
P1
    docker run --rm -v "$DATAVOL:/data" -v "$TOKJSON:/tokens.json:ro" alpine \
      sh -c 'cp /tokens.json /data/appservice-tokens.json && chmod 600 /data/appservice-tokens.json'
    echo "==> AppService tokens：新生成并持久化到数据卷（appservice-tokens.json）"
  fi
  AS_TOKEN=$(python -c 'import json;print(json.load(open("'"$TOKJSON"'"))["as_token"])')
  HS_TOKEN=$(python -c 'import json;print(json.load(open("'"$TOKJSON"'"))["hs_token"])')
  rm -f "$TOKJSON"
  python - "$ENVF" "$AS_TOKEN" "$HS_TOKEN" <<'P2'
import sys
path, as_tok, hs_tok = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path, encoding='utf-8').read().splitlines()
extra = {
  'AGENTTEAMS_MATRIX_APPSERVICE_ENABLED': 'true',
  'AGENTTEAMS_MATRIX_APPSERVICE_ID': 'agentteams-beta-ctrl',
  'AGENTTEAMS_MATRIX_APPSERVICE_SENDER_LOCALPART': 'agentteams-ctrl',
  'AGENTTEAMS_MATRIX_APPSERVICE_AS_TOKEN': as_tok,
  'AGENTTEAMS_MATRIX_APPSERVICE_HS_TOKEN': hs_tok,
  'AGENTTEAMS_MATRIX_APPSERVICE_CONTROLLER_URL': 'http://agentteams-beta-controller:8090',
  'AGENTTEAMS_MATRIX_APPSERVICE_USER_NAMESPACE_REGEX': '@mergepilot-.*',
}
seen = set(); out = []
for l in lines:
    k = l.split('=', 1)[0]
    if k in extra:
        out.append(k + '=' + extra[k]); seen.add(k)
    else:
        out.append(l)
for k, v in extra.items():
    if k not in seen:
        out.append(k + '=' + v)
open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
P2
  echo "    AppService env 已注入（ENABLED=true + 7 项）"
fi

# ---- 标准增量 2：LLM key（stdin 提供，先验活） ----
if [ "$LLM_KEY_STDIN" = "1" ]; then
  LLM_KEY=$(head -c 4096 | tr -d '\r\n')
  [ -n "$LLM_KEY" ] || { echo "FATAL: stdin 未提供 LLM key" >&2; exit 1; }
  HTTP=$(printf '%s' "$LLM_KEY" | python -c '
import sys, json, urllib.request
key = sys.stdin.read().strip()
req = urllib.request.Request("https://api.deepseek.com/chat/completions",
  data=json.dumps({"model":"deepseek-chat","messages":[{"role":"user","content":"ping"}],"max_tokens":1}).encode(),
  headers={"Authorization":"Bearer "+key, "content-type":"application/json"})
try:
    print(urllib.request.urlopen(req, timeout=20).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("ERR")
')
  [ "$HTTP" = "200" ] || { echo "FATAL: LLM key 验活失败（HTTP=$HTTP），拒绝写入（401=已撤销旧值）" >&2; exit 1; }
  python - "$ENVF" "$LLM_KEY" <<'P3'
import sys
path, key = sys.argv[1], sys.argv[2]
lines = open(path, encoding='utf-8').read().splitlines()
out, done = [], False
for l in lines:
    if l.startswith('AGENTTEAMS_OPENAI_API_KEY='):
        out.append('AGENTTEAMS_OPENAI_API_KEY=' + key); done = True
    else:
        out.append(l)
if not done:
    out.append('AGENTTEAMS_OPENAI_API_KEY=' + key)
open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
P3
  echo "    LLM key：验活 200，env 已替换（值不回显）"
else
  CURKEY=$(docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^AGENTTEAMS_OPENAI_API_KEY=' | cut -d= -f2- || true)
  if [ -n "$CURKEY" ]; then
    HTTP=$(printf '%s' "$CURKEY" | python -c '
import sys, json, urllib.request
key = sys.stdin.read().strip()
req = urllib.request.Request("https://api.deepseek.com/chat/completions",
  data=json.dumps({"model":"deepseek-chat","messages":[{"role":"user","content":"ping"}],"max_tokens":1}).encode(),
  headers={"Authorization":"Bearer "+key, "content-type":"application/json"})
try:
    print(urllib.request.urlopen(req, timeout=20).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("ERR")
')
    echo "    LLM key 现值验活：HTTP=$HTTP $([ "$HTTP" = "200" ] && echo '(healthy)' || echo '(REVOKED/异常 —— 建议 --llm-key-stdin 换现役值，否则重建后 env 仍携带失效 key)')"
  fi
fi

# ---- 标准增量 3：worker 镜像 pin ----
if [ -n "$WORKER_IMAGE" ]; then
  python - "$ENVF" "$WORKER_IMAGE" <<'P4'
import sys
path, ref = sys.argv[1], sys.argv[2]
lines = open(path, encoding='utf-8').read().splitlines()
out, done = [], False
for l in lines:
    if l.startswith('AGENTTEAMS_QWENPAW_WORKER_IMAGE='):
        out.append('AGENTTEAMS_QWENPAW_WORKER_IMAGE=' + ref); done = True
    else:
        out.append(l)
if not done:
    out.append('AGENTTEAMS_QWENPAW_WORKER_IMAGE=' + ref)
open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
P4
  echo "    worker 镜像 pin 已更新：$WORKER_IMAGE"
fi

if [ "$APPLY" != "1" ]; then
  echo "==> DRY-RUN 结束（未做任何变更）。执行请加 --apply"
  exit 0
fi

echo "==> 受控重建：rename→run 新容器→healthz→（失败自动回滚）"
PREV="$CTRL-prev-$TS"
docker rename "$CTRL" "$PREV"
docker stop -t 30 "$PREV" > /dev/null

ALIAS_ARGS=()
for a in $ALIASES; do [ -n "$a" ] && ALIAS_ARGS+=(--network-alias "$a"); done
if MSYS_NO_PATHCONV=1 docker run -d --name "$CTRL" \
  --network "$NET" "${ALIAS_ARGS[@]}" \
  $PORTS $BINDS \
  --env-file "$ENVF" \
  --restart "${RESTART:-no}" \
  "$IMAGE" > /dev/null; then
  echo "    新容器已启动，等待 healthz（最多 120s）..."
  OK=0
  APIPORT=$(docker inspect "$CTRL" --format '{{range $p, $b := .HostConfig.PortBindings}}{{if eq $p "8090/tcp"}}{{range $b}}{{.HostPort}}{{end}}{{end}}{{end}}')
  for i in $(seq 1 24); do
    sleep 5
    if curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${APIPORT:-18190}/healthz" | grep -q 200; then OK=1; break; fi
    S=$(docker inspect "$CTRL" --format '{{.State.Status}}' 2>/dev/null || echo gone)
    [ "$S" = "running" ] || break
  done
  if [ "$OK" = "1" ]; then
    echo "    healthz 200 ✓"
    if [ "$KEEP_OLD" = "0" ]; then docker rm "$PREV" > /dev/null && echo "    旧容器 $PREV 已删除（快照留存 $EV/ctrl-inspect-$TS.json）"
    else echo "    旧容器保留：$PREV"; fi
    echo "UPDATE-CTRL OK"
    exit 0
  fi
fi

echo "  !! 新容器健康检查失败 —— 自动回滚到旧 env" >&2
docker rm -f "$CTRL" > /dev/null 2>&1 || true
docker rename "$PREV" "$CTRL"
docker start "$CTRL" > /dev/null
echo "ROLLBACK DONE：旧容器已恢复（$CTRL）" >&2
exit 1
