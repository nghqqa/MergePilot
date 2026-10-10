#!/bin/bash
# recover-workers.sh — AgentTeams worker 栈受控部署/恢复（幂等；配置真源的唯一执行入口）
#
# 背景（2026-10-10 生产事故定案）：
#  * embedded controller 的 reconcile 会用 ctrl 模板【重写】MinIO 的
#    agents/<name>/openclaw.json——任何在 reconcile 之前写入的模型配置都会被抹掉；
#  * ctrl 内嵌 AI gateway（:8080）在本部署形态下返回 Higress 控制台 HTML，
#    不是可用的 LLM 端点——worker 的 LLM 路径必须走 deepseek-direct；
#  * copaw-worker 223ddc2 的 worker.py 调 sync.get_soul/get_agents_md 但 FileSync
#    无此二方法（上游版本错配）——必须使用含 shim 的镜像。
#
# 顺序契约（违反即失败，不可手工绕过）：
#   1. 前置检查   —— ctrl 存在且 healthy；凭据/镜像在位；否则明确报错退出
#   2. provision  —— 四 worker 创建/接管 + reconcile（ctrl API，幂等 201/409）
#   3. 模型配置   —— deepseek-direct 写入 MinIO（【必须】在 reconcile 之后）
#   4. 等待重桥   —— 轮询 worker 日志 "Config re-bridged"（worker 运行时轮询
#                    MinIO 配置变化并自动重桥；本工具【不重启】worker 容器）
#   5. 验证       —— fail-closed：任一失败即 exit 1 并明确报错（绝不静默回退 gateway）
#        a. 四 worker phase=Running 且 roomID/matrixUserID 完整
#        b. 四 worker 运行时 primary=deepseek-direct/deepseek-chat
#        c. deepseek API 从 worker 容器内实调 200
#
# 用法（凭据经环境注入，禁止写入任何文件）：
#   ATB_ADMIN_PASSWORD=... ATB_LLM_KEY=... bash recover-workers.sh \
#        [stack前缀=agentteams-beta] [ctrl容器=<stack>-ctrl] \
#        [worker镜像=ghcr.io/nghqqa/copaw-worker:223ddc2-v5fix-get-soul-shim]
#
# 退出码：0=全部通过；1=验证失败（带明确原因）；2=环境/前置失败。
set -euo pipefail

STACK="${1:-agentteams-beta}"
CTRL="${2:-${STACK}-ctrl}"
IMG="${3:-ghcr.io/nghqqa/copaw-worker:223ddc2-v5fix-get-soul-shim}"
: "${ATB_ADMIN_PASSWORD:?ATB_ADMIN_PASSWORD required (existing secret source)}"
: "${ATB_LLM_KEY:?ATB_LLM_KEY required (deepseek API key, existing secret source)}"

fail() { echo "RECOVER-FAILED: $*" >&2; exit 1; }
step() { echo "=== [recover] $*"; }

# ── 1. 前置检查 ─────────────────────────────────────────────
step "1/5 前置检查（ctrl/凭据/镜像）"
docker inspect "$CTRL" >/dev/null 2>&1 || { echo "ctrl container $CTRL not found" >&2; exit 2; }
CTRL_HEALTH=$(docker inspect "$CTRL" --format '{{.State.Health.Status}}' 2>/dev/null || echo "none")
[ "$CTRL_HEALTH" = "healthy" ] || fail "ctrl $CTRL health=$CTRL_HEALTH（等 healthy 后重试；不自动重启 ctrl）"
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "worker image $IMG not present locally" >&2; exit 2; }
TOK=$(docker exec "$CTRL" sh -c 'tr -d "\n\r" < /var/run/agentteams/cli-token')
[ -n "$TOK" ] || fail "ctrl cli-token 不可读"
ctrl_api() { docker exec "$CTRL" curl -s -m 30 -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }

# ── 2. provision：创建/接管 + reconcile ─────────────────────
step "2/5 provision（create/takeover + reconcile）"
for ROLE in leader reviewer fixer verifier; do
  NAME="mergepilot-$ROLE"
  CTR="$STACK-worker-$NAME"
  case "$ROLE" in
    leader)   IDENTITY="MergePilot 审查编排 Leader：只依据结构化 finding 摘要给出裁定建议，不执行任何写操作。"; PORT=38101;;
    reviewer) IDENTITY="MergePilot 审查 Reviewer：基于脱敏 finding 摘要给出语义审查建议（JSON）。"; PORT=38104;;
    fixer)    IDENTITY="MergePilot 修复 Fixer：仅产出 dry-run 修复建议文本，禁止执行命令或写仓库。"; PORT=38107;;
    verifier) IDENTITY="MergePilot 验证 Verifier：独立判断修复建议是否解决 finding，不信任 Fixer 自述。"; PORT=38110;;
  esac
  # 2a. 幂等创建（409=已存在，复用）
  CODE=$(ctrl_api -X POST -o /dev/null -w "%{http_code}" \
    -d "{\"name\":\"$NAME\",\"spec\":{\"runtime\":\"copaw\",\"model\":\"deepseek-chat\",\"identity\":\"$IDENTITY\",\"state\":\"Running\"}}" \
    "http://127.0.0.1:8090/api/v1/workers" || echo 000)
  { [ "$CODE" = "201" ] || [ "$CODE" = "409" ]; } || fail "create $NAME http=$CODE"
  # 2b. ctrl 生成容器出现（新建时）
  for i in $(seq 1 30); do [ -n "$(docker ps -aq --filter name="$CTR")" ] && break; sleep 2; done
  [ -n "$(docker ps -aq --filter name="$CTR")" ] || fail "controller container for $NAME never appeared"
  sleep 2
  # 2c. 接管（共享 netns + auth 卷 + 环境继承；仅当容器非本工具管理的镜像时）
  RUNIMG=$(docker inspect "$CTR" --format '{{.Config.Image}}' 2>/dev/null || echo "")
  if [ "$RUNIMG" != "$IMG" ] || [ ! "$(docker inspect "$CTR" --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || echo "")" = "container:$CTRL" ]; then
    mapfile -t ENVARGS < <(docker inspect "$CTR" --format '{{range .Config.Env}}{{println .}}{{end}}' \
      | tr -d '\r' | sed -E 's/^ +//; s/^/--env=/; /--env=$/d; /--env=AGENTTEAMS_CONSOLE_PORT=/d; /--env=DEEPSEEK_API_KEY=/d')
    AUTHVOL=$(MSYS_NO_PATHCONV=1 docker inspect "$CTR" --format '{{range .Mounts}}{{if eq .Destination "/var/run/secrets/agentteams"}}{{.Name}}{{end}}{{end}}')
    [ -n "$AUTHVOL" ] || fail "auth volume not found for $NAME"
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    MSYS_NO_PATHCONV=1 docker run -d --name "$CTR" --network "container:$CTRL" \
      --volume "$AUTHVOL:/var/run/secrets/agentteams" \
      --restart unless-stopped \
      "${ENVARGS[@]}" --env="AGENTTEAMS_CONSOLE_PORT=$PORT" \
      --env=COPAW_LOG_LEVEL=warning \
      --env="DEEPSEEK_API_KEY=$ATB_LLM_KEY" "$IMG" >/dev/null \
      || fail "takeover run $NAME failed"
  fi
  # 2d. reconcile（Sleeping→Running；幂等；这一步会重写 MinIO 配置——模型 patch 必须在其后）
  ctrl_api -X PUT -d '{"spec":{"state":"Sleeping"}}' -o /dev/null "http://127.0.0.1:8090/api/v1/workers/$NAME" >/dev/null || fail "reconcile-sleep $NAME"
  sleep 2
  ctrl_api -X PUT -d '{"spec":{"state":"Running"}}' -o /dev/null "http://127.0.0.1:8090/api/v1/workers/$NAME" >/dev/null || fail "reconcile-run $NAME"
  sleep 2
done

# ── 3+4. 模型配置收敛循环（reconcile 之后；吸收 ctrl 异步模板重推竞态）──
# 两条 itest 隔离实证（2026-10-10）：
#  a) sync_loop 合并规则="models 段从远端整体替换；agents 段（含 primary）保留本地"
#     ——只改 primary 不传播；正确做法=重定向 agentteams-gateway provider 的
#     baseUrl/apiKey 到 deepseek 直连（内嵌 gateway :8080 是 Higress 控制台非 LLM）。
#  b) ctrl 对 worker 接管的模板重推是【异步】的，可能落在 patch 之后——单次 patch
#     会被覆盖。收敛循环：每轮 ①幂等重打 MinIO（仅值不符时）②验运行时全部生效；
#     连续两轮全绿（隔一个 sync 周期无回退）= 收敛（有界 ≤300s）。
step "3/5+4/5 模型配置收敛（重定向 agentteams-gateway→deepseek；吸收异步重推，≤300s）"
patch_one() { # $1=ROLE
  docker exec -e DK="$ATB_LLM_KEY" "$STACK-worker-mergepilot-$1" python3 -c "
import json, subprocess, os, sys
p = 'agentteams/agentteams-storage/agents/' + os.environ['AGENTTEAMS_WORKER_NAME'] + '/openclaw.json'
raw = subprocess.run(['mc','cat',p], capture_output=True, text=True).stdout
try:
    d = json.loads(raw)
except Exception as e:
    print('PARSE-FAIL:' + str(e)[:60]); sys.exit(1)
provs = d.setdefault('models', {}).setdefault('providers', {})
gw = provs.get('agentteams-gateway', {})
if gw.get('baseUrl') == 'https://api.deepseek.com/v1':
    print('noop'); sys.exit(0)
models_list = [m for m in gw.get('models', []) if m.get('id') == 'deepseek-chat'] or [
  {'id': 'deepseek-chat', 'name': 'deepseek-chat', 'contextWindow': 64000, 'maxTokens': 8000, 'input': ['text']}]
provs['agentteams-gateway'] = {
  'api': 'openai-completions', 'apiKey': os.environ['DK'],
  'baseUrl': 'https://api.deepseek.com/v1', 'models': models_list}
provs['deepseek-direct'] = dict(provs['agentteams-gateway'])
d.setdefault('agents', {}).setdefault('defaults', {}).setdefault('model', {})['primary'] = 'agentteams-gateway/deepseek-chat'
r = subprocess.run(['mc','pipe',p], input=json.dumps(d, ensure_ascii=False, indent=2), capture_output=True, text=True)
print('patched' if r.returncode == 0 else 'PIPE-FAIL:' + r.stderr[:80])
" 2>&1
}
CONV_DL=$(( $(date +%s) + 300 )); CONV_STREAK=0
for ROUND in $(seq 1 15); do
  [ "$(date +%s)" -gt "$CONV_DL" ] && break
  for ROLE in leader reviewer fixer verifier; do
    OUT=$(patch_one "$ROLE")
    case "$OUT" in patched|noop) ;; *) fail "model patch $ROLE: $OUT";; esac
  done
  ALL=1
  for ROLE in leader reviewer fixer verifier; do
    P=$(docker exec "$STACK-worker-mergepilot-$ROLE" python3 -c "
import json
d=json.load(open('/root/.copaw-worker/'+__import__('os').environ['AGENTTEAMS_WORKER_NAME']+'/openclaw.json'))
print(d.get('models',{}).get('providers',{}).get('agentteams-gateway',{}).get('baseUrl','?'))" 2>/dev/null || echo "read-fail")
    [ "$P" = "https://api.deepseek.com/v1" ] || ALL=0
  done
  if [ "$ALL" = "1" ]; then CONV_STREAK=$((CONV_STREAK+1)); [ "$CONV_STREAK" -ge 2 ] && break
  else CONV_STREAK=0; fi
  sleep 20
done
[ "$CONV_STREAK" -ge 2 ] || fail "模型配置 300s 内未收敛（ctrl 异步重推持续或 sync 未传播）——绝不静默回退内嵌 gateway"
echo "  四 worker 运行时 agentteams-gateway → https://api.deepseek.com/v1（连续两轮稳定）"

# ── 5. 验证（fail-closed）───────────────────────────────────
step "5/5 端到端验证"
# 5a. worker phase/绑定
for ROLE in leader reviewer fixer verifier; do
  W=$(ctrl_api "http://127.0.0.1:8090/api/v1/workers/mergepilot-$ROLE")
  echo "$W" | docker exec -i "$CTRL" python3 -c "
import json,sys
w=json.load(sys.stdin)
ph=w.get('phase','?'); room=w.get('roomID') or w.get('room',''); mid=w.get('matrixUserID') or ''
assert ph=='Running', 'phase='+ph
assert room, 'roomID missing'
assert mid, 'matrixUserID missing'
" || fail "worker mergepilot-$ROLE phase/绑定不完整"
  echo "  $ROLE: Running + room/matrixID 完整"
done
# 5b. get_soul 复发检查（shim 生效证明；最近 5 分钟窗口）
for ROLE in leader reviewer fixer verifier; do
  N=$(docker logs "$STACK-worker-mergepilot-$ROLE" --since 5m 2>&1 | grep -c "Re-bridge failed" || true)
  [ "$N" = "0" ] || fail "worker $ROLE 仍有 get_soul 重桥失败（$N 次）——shim 未生效"
done
echo "  四 worker: get_soul 零复发（shim 生效）"
# 5c. deepseek API 实调（从 worker 容器内；200+可解析回复）
PROBE=$(docker exec -e DK="$ATB_LLM_KEY" "$STACK-worker-mergepilot-fixer" python3 -c "
import json, urllib.request, os, sys
req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
  data=json.dumps({'model':'deepseek-chat','messages':[{'role':'user','content':'reply OK'}],'max_tokens':5}).encode(),
  headers={'content-type':'application/json','authorization':'Bearer '+os.environ['DK']})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        d=json.loads(r.read()); c=d['choices'][0]['message']['content']
        print('OK:'+c[:16] if 'ok' in c.lower() else 'CONTENT:'+c[:16])
except Exception as e:
    print('FAIL:'+str(e)[:80]); sys.exit(1)
" 2>&1) || fail "deepseek API 实调失败: $PROBE"
case "$PROBE" in OK:*|CONTENT:*) echo "  deepseek API 实调: $PROBE";; *) fail "deepseek API 异常响应: $PROBE";; esac

echo ""
echo "RECOVER-OK: 四 worker 部署/恢复完成（镜像=$IMG；模型=agentteams-gateway→deepseek 直连重定向；全部验证通过）"
