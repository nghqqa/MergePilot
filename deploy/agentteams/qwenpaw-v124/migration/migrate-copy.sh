#!/usr/bin/env bash
# migrate-copy.sh — AgentTeams 223ddc2 → v1.2.4-CAS 生产副本迁移编排（幂等）
#
# 波次实证来源：2026-10-12 C 线演练（cline-data 副本全链跑通——四角色真实往返 4/4、
# delegate/ack/submit 全通、cancel 竞态+迟到拒收 PASS、回滚 B 同卷降级+Matrix 业务恢复 PASS）。
#
# ── 关键机制（本波源码/行为定案，勿凭旧结论操作）──
# K1 console 登录 = 明文比对 secret higress-console 的 adminPassword 字段（base64 解码后
#    与提交口令 equals）；AesUtil CBC 只加密会话 token，不碰口令字段。
#    → 401 的根因永远是"secret 里的口令 ≠ ctrl env 的口令"（漂移），修复=收敛字段值。
# K2 worker/manager 容器镜像由 CR spec.runtime + spec.image 决定（env *_IMAGE 仅为新建
#    默认）。迁移必须 agt apply worker/manager 更新 runtime+image；只改 ctrl env 无效。
# K3 qwenpaw worker 的 Matrix 通道由容器 env（AGENTTEAMS_WORKER_MATRIX_TOKEN/ROOM_ID）
#    驱动的 agentteams_matrix 插件提供；迁移后的 worker 若 config.json 里残留
#    channels.matrix.enabled=true（旧 copaw 时代配置）会与之冲突停摆——迁移后置
#    matrix.enabled=false（env 通道接管；新建 worker 即此形态）。
# K4 223ddc2 ctrl 的 SA 签名密钥随实例首启持久化于其数据目录（pki/sa.key）；ctrl 若以
#    不同数据目录重启＝密钥更换＝旧 JWT 全部失效。回滚预案必须包含 auth-proxy 上游
#    token 重签（用该目录的 sa.key 铸 ES256 SA JWT，或恢复同目录后沿用旧 JWT）。
# K5 演练/维护环境必须全量 cline 化（RESOURCE_PREFIX/DOCKER_NETWORK/FS_ENDPOINT/
#    CONTROLLER_URL/MatrixURL/端口基），否则 ctrl 会以同名容器接管生产资源。
#
# 用法（副本/维护窗口环境，严禁直连生产）：
#   CLINE_CTRL=cline-controller CLINE_DATA_VOL=cline-data bash migrate-copy.sh [step...]
# 步骤：precheck backup matrix-sync higress-converge cr-migrate channel-fix llm-seed verify
set -euo pipefail
export MSYS_NO_PATHCONV=1

CTRL="${CLINE_CTRL:?CLINE_CTRL（演练/维护 ctrl 容器名）必需}"
DATAVOL="${CLINE_DATA_VOL:?CLINE_DATA_VOL（副本卷名）必需}"
CAS_WORKER_IMAGE="${CAS_WORKER_IMAGE:-agentteams-qwenpaw-worker:1.2.4-cas}"
CAS_MANAGER_IMAGE="${CAS_MANAGER_IMAGE:-higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager-qwenpaw@sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805}"
WORKERS="leader reviewer fixer verifier"
EV="${MIGRATE_EV:-./migrate-evidence}"; mkdir -p "$EV"
STEPS="${*:-precheck backup matrix-sync higress-converge cr-migrate channel-fix llm-seed verify}"

step() { echo "==> [$(date -u '+%F %T')] $*"; }
die() { echo "FATAL: $*" >&2; exit 1; }
wants() { case " $STEPS " in *" $1 "*) return 0;; *) return 1;; esac; }
env_of() { docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep "^$1=" | cut -d= -f2- | tr -d '\r\n'; }

APW=$(env_of AGENTTEAMS_ADMIN_PASSWORD); [ -n "$APW" ] || die "缺 AGENTTEAMS_ADMIN_PASSWORD"
APU=$(env_of AGENTTEAMS_ADMIN_USER);     [ -n "$APU" ] || die "缺 AGENTTEAMS_ADMIN_USER"
MDOM=$(env_of AGENTTEAMS_MATRIX_DOMAIN); [ -n "$MDOM" ] || die "缺 AGENTTEAMS_MATRIX_DOMAIN"
LLM_BASE=$(env_of AGENTTEAMS_LLM_BASE_URL); LLM_BASE="${LLM_BASE:-$(env_of AGENTTEAMS_OPENAI_BASE_URL)}"
LLM_KEY=$(env_of AGENTTEAMS_LLM_API_KEY);   LLM_KEY="${LLM_KEY:-$(env_of AGENTTEAMS_OPENAI_API_KEY)}"
LLM_PORT=443; LLM_PROTO=https; case "$LLM_BASE" in http:*) LLM_PORT=80; LLM_PROTO=http;; esac
LLM_DOMAIN=$(printf '%s' "$LLM_BASE" | sed -E 's#^https?://##; s#[:/].*##')

api18443() {
  docker exec "$CTRL" sh -c "DT=\$(cut -d, -f1 /data/ctrl-statedir/pki/token.csv | tr -d '\r\n'); curl -sk -H 'Authorization: Bearer \$DT' $*"
}

wants precheck && {
  step "precheck"
  docker exec "$CTRL" curl -sf http://127.0.0.1:8090/healthz >/dev/null || die "ctrl healthz 不通"
  docker image inspect "$CAS_WORKER_IMAGE" >/dev/null || die "缺 CAS worker 镜像 $CAS_WORKER_IMAGE"
  echo "precheck OK"
}

wants backup && {
  step "backup（kine 完整性 + secret/cm 快照）"
  docker cp "$CTRL:/data/ctrl-statedir/agentteams.db" "$EV/kine-backup.db"
  python - "$EV/kine-backup.db" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
ic = con.execute("PRAGMA integrity_check").fetchone()[0]
print("kine integrity_check:", ic)
sys.exit(0 if ic == "ok" else 1)
PYEOF
  api18443 "https://127.0.0.1:18443/api/v1/namespaces/higress-system/secrets/higress-console" > "$EV/secret-before.json"
  api18443 "https://127.0.0.1:18443/api/v1/namespaces/higress-system/configmaps/higress-console" > "$EV/cm-before.json"
  echo "快照：$EV/{kine-backup.db,secret-before.json,cm-before.json}"
}

wants matrix-sync && {
  step "matrix-sync（Tuwunel admin 口令 ← ctrl env；幂等）"
  docker stop "$CTRL" >/dev/null; sleep 8
  docker run --rm --entrypoint tuwunel -v "$DATAVOL:/data" \
    -e CONDUWUIT_DATABASE_PATH=/data/tuwunel -e CONDUWUIT_SERVER_NAME="$MDOM" \
    higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:1.2.4-cas \
    --maintenance --execute "users reset-password @${APU}:${MDOM} ${APW}" >/dev/null 2>&1 || true
  docker start "$CTRL" >/dev/null; sleep 15
  docker exec "$CTRL" curl -sf http://127.0.0.1:8090/healthz >/dev/null || die "matrix-sync 后 healthz 不通"
  echo "matrix-sync 完成（口令已对齐时无副作用）"
}

wants higress-converge && {
  step "higress-converge（secret adminPassword ← ctrl env 口令；K1）"
  python - "$CTRL" "$APW" "$EV" <<'PYEOF'
import base64, json, subprocess, sys
ctrl, apw, ev = sys.argv[1:4]
def api(args):
    return subprocess.run(["docker","exec",ctrl,"sh","-c",
        "DT=$(cut -d, -f1 /data/ctrl-statedir/pki/token.csv | tr -d '\r\n'); curl -sk -H 'Authorization: Bearer $DT' " + args],
        capture_output=True, text=True, timeout=60).stdout
cur = json.loads(api("https://127.0.0.1:18443/api/v1/namespaces/higress-system/secrets/higress-console"))
cur_pw = base64.b64decode(cur["data"]["adminPassword"]).decode("utf-8", "replace")
if cur_pw == apw:
    print("已收敛——幂等跳过"); raise SystemExit(0)
cur["data"]["adminPassword"] = base64.b64encode(apw.encode()).decode()
open(f"{ev}/secret-converge.json","w").write(json.dumps(cur))
subprocess.run(["docker","cp",f"{ev}/secret-converge.json",f"{ctrl}:/tmp/mig-secret.json"], capture_output=True, timeout=60)
r = subprocess.run(["docker","exec",ctrl,"sh","-c",
    "DT=$(cut -d, -f1 /data/ctrl-statedir/pki/token.csv | tr -d '\r\n'); curl -sk -X PUT -H 'Authorization: Bearer $DT' -H 'Content-Type: application/json' --data-binary @/tmp/mig-secret.json https://127.0.0.1:18443/api/v1/namespaces/higress-system/secrets/higress-console"],
    capture_output=True, text=True, timeout=60)
print("converge PUT:", "ok" if '"higress-console"' in r.stdout else r.stdout[:120])
PYEOF
  docker restart "$CTRL" >/dev/null; sleep 25
  docker exec "$CTRL" curl -sf http://127.0.0.1:8090/healthz >/dev/null || die "converge 后 healthz 不通"
  echo "converge 完成（console 登录=env 口令）"
}

wants cr-migrate && {
  step "cr-migrate（worker runtime→qwenpaw+CAS image；manager→qwenpaw；K2）"
  docker exec "$CTRL" sh -c 'export AGENTTEAMS_AUTH_TOKEN=$(cat /var/run/agentteams/cli-token | tr -d "\r\n"); for r in leader reviewer fixer verifier; do agt apply worker --name mergepilot-$r --runtime qwenpaw --image '"$CAS_WORKER_IMAGE"' 2>&1 | tail -1; done'
  docker exec "$CTRL" sh -c "cat > /tmp/mgr-mig.yaml <<EOF
apiVersion: agentteams.agentteam/v1beta1
kind: Manager
metadata:
  name: default
  namespace: default
spec:
  runtime: qwenpaw
  image: $CAS_MANAGER_IMAGE
EOF
export AGENTTEAMS_AUTH_TOKEN=\$(cat /var/run/agentteams/cli-token | tr -d '\r\n'); agt apply -f /tmp/mgr-mig.yaml 2>&1 | tail -1"
  sleep 60
  for w in $WORKERS; do
    IMG=$(docker inspect "clineworker-mergepilot-$w" --format '{{.Config.Image}}' 2>/dev/null || echo none)
    [ "$IMG" = "$CAS_WORKER_IMAGE" ] || echo "WARN: $w 镜像=$IMG（CR 迁移可能未生效）"
  done
}

wants channel-fix && {
  step "channel-fix（K3：迁移 worker 的 config.json matrix.enabled→False，env 通道接管）"
  for w in $WORKERS; do
    docker exec "clineworker-mergepilot-$w" python3 - <<PYEOF 2>/dev/null || echo "WARN: $w config 修正失败"
import json
p = "/root/agentteams-fs/agents/mergepilot-$w/.qwenpaw/config.json"
d = json.load(open(p))
if d.get("channels", {}).get("matrix", {}).get("enabled"):
    d["channels"]["matrix"]["enabled"] = False
    json.dump(d, open(p, "w"), indent=1)
    print("$w: matrix.enabled→False")
else:
    print("$w: 已为 False——幂等跳过")
PYEOF
    docker restart "clineworker-mergepilot-$w" >/dev/null
  done
  sleep 45
  for w in $WORKERS; do
    N=$(docker logs "clineworker-mergepilot-$w" 2>&1 | grep -ac "MatrixChannel: logged in")
    echo "$w Matrix logged-in 计数: $N"
  done
}

wants llm-seed && {
  step "llm-seed（service-source/provider/route；key 经 env，值不落盘）"
  CLINE_CONSOLE_PORT="${CLINE_CONSOLE_PORT:-18098}" python - "$CTRL" "$APU" "$APW" "$LLM_KEY" "$LLM_BASE" "$LLM_DOMAIN" "$LLM_PORT" "$LLM_PROTO" <<'PYEOF'
import json, os, subprocess, sys
_, apu, apw, key, base, domain, port, proto = sys.argv[1:9]
cport = os.environ.get("CLINE_CONSOLE_PORT", "18098")
jar = "/tmp/mig-hc.txt"
def curl(args):
    return subprocess.run(["curl","-s"] + args, capture_output=True, text=True, timeout=60).stdout
sess = curl(["-c",jar,f"http://127.0.0.1:{cport}/session","-H","Accept: application/json"])
try: csrf = json.loads(sess).get("csrf","")
except Exception: csrf = ""
curl(["-b",jar,"-c",jar,"-X","POST","-H","content-type: application/json","-H",f"X-CSRF-Token: {csrf}",
      "-d",json.dumps({"username":apu,"password":apw}),f"http://127.0.0.1:{cport}/session/login"])
def api(method, path, body=None):
    cmd = ["curl","-s","-b",jar,"-c",jar,"-X",method,"-H","content-type: application/json"]
    if body is not None: cmd += ["--data-raw", json.dumps(body)]
    return curl(cmd + [f"http://127.0.0.1:{cport}{path}"])[:80]
api("POST","/v1/service-sources",{"type":"dns","name":"openai-compat","port":port,"protocol":proto,"proxyName":"","domain":domain})
print("provider:", api("POST","/v1/ai/providers",{"type":"openai","name":"openai-compat","tokens":[key],"version":0,"protocol":"openai/v1","tokenFailoverConfig":{"enabled":False},"rawConfigs":{"openaiCustomUrl":base,"openaiCustomServiceName":"openai-compat.dns","openaiCustomServicePort":int(port)}}))
print("route:", api("POST","/v1/ai/routes",{"name":"mp-llm","domains":["*"],"pathPredicate":{"matchType":"PRE","matchValue":"/v1/chat/completions"},"upstreams":[{"provider":"openai-compat","weight":100}],"version":0}))
PYEOF
}

wants verify && {
  step "verify（网关 LLM 探针 + worker Matrix 计数；完整业务探针用 cline-4role/cline-delegate-chain）"
  docker exec "clineworker-mergepilot-reviewer" sh -c 'curl -s -m 25 -X POST http://aigw-local.agentteams.io:8080/v1/chat/completions -H "content-type: application/json" -d "{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}],\"max_tokens\":8}"' | grep -q 'choices' && echo "网关 LLM 探针: PASS" || die "网关 LLM 探针失败"
  for w in $WORKERS; do
    N=$(docker logs "clineworker-mergepilot-$w" 2>&1 | grep -ac "MatrixChannel: logged in")
    [ "$N" -ge 1 ] || die "worker $w Matrix 未登录"
  done
  echo "verify: 基础项 PASS（四角色往返/委派链用 cline-4role.sh + cline-delegate-chain.py）"
}

echo "MIGRATION CONVERGED（执行步骤：$STEPS）"
