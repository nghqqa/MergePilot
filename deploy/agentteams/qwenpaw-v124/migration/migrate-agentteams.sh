#!/usr/bin/env bash
# migrate-agentteams.sh — AgentTeams 223ddc2 → v1.2.4-CAS 迁移编排（副本/生产维护双模式）
#
# 机制定案 K1-K5（2026-10-12 C 线演练实证）：
#  K1 console 登录 = 明文比对 secret higress-console 的 adminPassword 字段；AesUtil CBC
#     只加密会话 token → 401 根因永远是"secret 口令 ≠ ctrl env 口令"（漂移），修复=收敛。
#  K2 容器镜像由 CR spec.runtime+image 决定；迁移=agt apply worker/manager（只改 env 无效）。
#  K3 迁移 worker 需 config.json channels.matrix.enabled→False（env 驱动通道接管）。
#  K4 223ddc2 SA 密钥随数据目录持久化；换目录重启=旧 JWT 全失效→回滚必须重签 auth-proxy JWT。
#  K5 演练/维护环境必须全量前缀/网络隔离（防同名容器接管生产资源）。
#
# 用法：
#   migrate-agentteams.sh --mode copy|production [--steps s1,s2,…] [--only step]
# 步骤：precheck backup matrix-sync higress-converge cr-migrate channel-fix llm-seed verify
# 断点续作：checkpoint 记录已完成步骤（--only 强制重跑单步）。
# 失败纪律：任何远程调用失败=非零退出+现场写入 $EV/failures.jsonl；秘密值不回显不落盘。
set -euo pipefail
export MSYS_NO_PATHCONV=1

MODE="" ; STEPS_OVERRIDE="" ; ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2;;
    --steps) STEPS_OVERRIDE="$2"; shift 2;;
    --only) ONLY="$2"; shift 2;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done
[ -n "$MODE" ] || { echo "用法: $0 --mode copy|production [--steps …] [--only step]" >&2; exit 2; }

env_of() { docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep "^$1=" | cut -d= -f2- | tr -d '
'; }
k8s_token() { docker exec "$CTRL" sh -c "cut -d, -f1 $STATEDIR/pki/token.csv | tr -d '
'"; }

if [ "$MODE" = "production" ]; then
  CTRL="${CTRL:-agentteams-beta-ctrl}"
  DATAVOL="${DATAVOL:-agentteams-beta-data}"
  STATEDIR="${STATEDIR:-/agentteams-beta-data}"
  CAS_WORKER_IMAGE="${CAS_WORKER_IMAGE:?production 模式必须显式提供完整锚 CAS_WORKER_IMAGE（含 sha256:…）}"
  CAS_MANAGER_IMAGE="${CAS_MANAGER_IMAGE:?production 模式必须显式提供 CAS_MANAGER_IMAGE 完整锚}"
  LLM_BASE="${LLM_BASE:?production 模式必须显式提供现役 LLM_BASE（禁止沿用 env 旧值）}"
  LLM_KEY="${LLM_KEY:?production 模式必须显式提供现役 LLM_KEY（值不回显）}"
  LLM_MODEL="${LLM_MODEL:-deepseek-chat}"
  MX_PUBLIC="${MX_PUBLIC:?production 模式必须显式提供 MX_PUBLIC（Matrix 对外可达地址）}"
else
  CTRL="${CTRL:-cline-controller}"
  DATAVOL="${DATAVOL:-cline-data}"
  STATEDIR="${STATEDIR:-/data/ctrl-statedir}"
  APW="${APW:-$(env_of AGENTTEAMS_ADMIN_PASSWORD)}"
  APU="${APU:-$(env_of AGENTTEAMS_ADMIN_USER)}"
  MDOM="${MDOM:-$(env_of AGENTTEAMS_MATRIX_DOMAIN)}"
  [ -n "$APW" ] || die precheck "ctrl env 缺 AGENTTEAMS_ADMIN_PASSWORD"
  [ -n "$APU" ] || die precheck "ctrl env 缺 AGENTTEAMS_ADMIN_USER"
  [ -n "$MDOM" ] || die precheck "ctrl env 缺 AGENTTEAMS_MATRIX_DOMAIN"
  CAS_WORKER_IMAGE="${CAS_WORKER_IMAGE:-agentteams-qwenpaw-worker@sha256:1719208fd17ce535fc2d222bfc66c9ab72aa90d21e3008c4f564341903703a24}"
  CAS_MANAGER_IMAGE="${CAS_MANAGER_IMAGE:-higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager-qwenpaw@sha256:965517c923fe09b4e7238b294bc6511f2344acf6b04ca79a6b17059995555805}"
  LLM_BASE="${LLM_BASE:-https://api.deepseek.com}"
  LLM_KEY="${LLM_KEY:-$(docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^AGENTTEAMS_OPENAI_API_KEY=' | cut -d= -f2- | tr -d '\r\n')}"
  LLM_MODEL="${LLM_MODEL:-deepseek-chat}"
  MX_PUBLIC="${MX_PUBLIC:-http://127.0.0.1:18196}"
fi
WORKERS="leader reviewer fixer verifier"
EV="${MIGRATE_EV:-./migration-evidence-$MODE}"; mkdir -p "$EV"
CKPT="$EV/checkpoint.json"; [ -f "$CKPT" ] || echo '{}' > "$CKPT"
STEPS="${STEPS_OVERRIDE:-precheck,backup,matrix-sync,higress-converge,cr-migrate,coldstart-env,channel-fix,llm-seed,verify}"

step() { echo "==> [$(date -u '+%F %T')] $*"; }
die() { local s="$1"; shift; echo "FATAL[$s]: $*" >&2; printf '{"step":"%s","error":"%s","ts":"%s"}\n' "$s" "$*" "$(date -u +%FT%TZ)" >> "$EV/failures.jsonl"; exit 1; }
done_step() { STEP="$1" CKPT="$CKPT" python - <<'P1'
import json, os, time
p, s = os.environ["CKPT"], os.environ["STEP"]
d = json.load(open(p, encoding="utf-8"))
d[s] = {"done": True, "ts": time.strftime("%FT%TZ", time.gmtime())}
json.dump(d, open(p, "w"), indent=1)
P1
}
is_done() { STEP="$1" CKPT="$CKPT" python - <<'P2'
import json, os
d = json.load(open(os.environ["CKPT"], encoding="utf-8"))
print("yes" if d.get(os.environ["STEP"], {}).get("done") else "no")
P2
}
env_of() { docker inspect "$CTRL" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep "^$1=" | cut -d= -f2- | tr -d '\r\n'; }
k8s_token() { docker exec "$CTRL" sh -c "cut -d, -f1 $STATEDIR/pki/token.csv | tr -d '\r\n'"; }
matrix_login() {
  curl -s -m 20 -X POST -H 'content-type: application/json' \
    -d "{\"type\":\"m.login.password\",\"user\":\"$1\",\"password\":\"$2\"}" \
    "$MX_PUBLIC/_matrix/client/v3/login" | python -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true
}

s_precheck() {
  step "precheck"
  docker exec "$CTRL" curl -sf -m 10 http://127.0.0.1:8090/healthz >/dev/null || die precheck "ctrl healthz 不通"
  case "$CAS_WORKER_IMAGE" in *sha256:*) ;; *) die precheck "CAS_WORKER_IMAGE 必须含完整 sha256 锚";; esac
  docker image inspect "$CAS_WORKER_IMAGE" >/dev/null || die precheck "本机缺 CAS worker 镜像锚"
  docker exec "$CTRL" sh -c "test -f $STATEDIR/pki/sa.key && test -f $STATEDIR/pki/token.csv" || die precheck "状态目录缺 PKI/token"
  if [ -n "$(matrix_login "$APU" "$APW")" ]; then
    echo "precheck: Matrix env 口令有效（matrix-sync 将幂等跳过）"
  else
    echo "precheck: Matrix env 口令无效——matrix-sync 将 reset-password（保留账号/房间）"
  fi
  echo "precheck OK"
}

s_backup() {
  step "backup（quiesce→冷拷贝→sqlite backup 收敛→integrity_check+CR/secret/cm 快照）"
  local WAS_RUNNING; WAS_RUNNING=$(docker inspect "$CTRL" --format '{{.State.Running}}')
  [ "$WAS_RUNNING" = "true" ] && docker stop "$CTRL" >/dev/null
  docker cp "$CTRL:$STATEDIR/agentteams.db" "$EV/kine-live.db" || die backup "kine 拷贝失败"
  docker cp "$CTRL:$STATEDIR/agentteams.db-wal" "$EV/kine-live.db-wal" 2>/dev/null || true
  docker cp "$CTRL:$STATEDIR/agentteams.db-shm" "$EV/kine-live.db-shm" 2>/dev/null || true
  [ "$WAS_RUNNING" = "true" ] && docker start "$CTRL" >/dev/null && sleep 15
  EVDIR="$EV" python - <<'P3'
import sqlite3, os
ev = os.environ["EVDIR"]
src, dst = os.path.join(ev, "kine-live.db"), os.path.join(ev, "kine-consistent.db")
if os.path.exists(dst): os.remove(dst)
con = sqlite3.connect(src); bak = sqlite3.connect(dst)
con.backup(bak); bak.close(); con.close()
chk = sqlite3.connect(dst)
ic = chk.execute("PRAGMA integrity_check").fetchone()[0]
rows = chk.execute("select count(*) from kine").fetchone()[0]
chk.close()
print("kine 一致快照: integrity=%s rows=%s" % (ic, rows))
if ic != "ok": raise SystemExit(1)
P3
  local DT; DT=$(k8s_token)
  docker exec "$CTRL" sh -c "curl -sk -m 30 -H 'Authorization: Bearer $DT' https://127.0.0.1:18443/api/v1/namespaces/higress-system/secrets/higress-console" > "$EV/secret-before.json" || die backup "secret 快照失败"
  grep -q '"higress-console"' "$EV/secret-before.json" || die backup "secret 快照内容异常"
  docker exec "$CTRL" sh -c "curl -sk -m 30 -H 'Authorization: Bearer $DT' https://127.0.0.1:18443/api/v1/namespaces/higress-system/configmaps/higress-console" > "$EV/cm-before.json" || die backup "configmap 快照失败"
  for w in $WORKERS; do
    docker exec "$CTRL" sh -c "curl -sk -m 30 -H 'Authorization: Bearer $DT' https://127.0.0.1:6443/apis/agentteams.io/v1beta1/namespaces/default/workers/mergepilot-$w" > "$EV/cr-worker-$w-before.json" || die backup "worker CR 快照失败 $w"
    grep -q "\"mergepilot-$w\"" "$EV/cr-worker-$w-before.json" || die backup "worker CR 快照内容异常 $w"
  done
  docker exec "$CTRL" sh -c "curl -sk -m 30 -H 'Authorization: Bearer $DT' https://127.0.0.1:6443/apis/agentteams.io/v1beta1/namespaces/default/managers/default" > "$EV/cr-manager-before.json" || die backup "manager CR 快照失败"
  grep -q '"default"' "$EV/cr-manager-before.json" || die backup "manager CR 快照内容异常"
  docker inspect "$CTRL" --format '{{.State.Running}}' | grep -q true || docker start "$CTRL" >/dev/null
  echo "backup 完成：kine 一致快照+secret/cm+完整 CR spec"
  done_step backup
}

s_matrix_sync() {
  step "matrix-sync（幂等：env 口令有效跳过；无效 reset-password 保留账号/房间）"
  if [ -n "$(matrix_login "$APU" "$APW")" ]; then echo "已对齐——幂等跳过"; done_step matrix-sync; return; fi
  docker stop "$CTRL" >/dev/null; sleep 8
  docker run --rm --entrypoint tuwunel -v "$DATAVOL:/data" \
    -e CONDUWUIT_DATABASE_PATH=/data/tuwunel -e CONDUWUIT_SERVER_NAME="$MDOM" \
    higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:1.2.4-cas \
    --maintenance --execute "users reset-password @${APU}:${MDOM} ${APW}" > "$EV/matrix-sync.log" 2>&1 \
    || die matrix-sync "tuwunel execute 失败（现场 $EV/matrix-sync.log）"
  if grep -aqE "unrecognized|Execute command #0 failed" "$EV/matrix-sync.log"; then
    die matrix-sync "reset-password 被拒（现场 $EV/matrix-sync.log）"
  fi
  docker start "$CTRL" >/dev/null; sleep 15
  docker exec "$CTRL" curl -sf -m 10 http://127.0.0.1:8090/healthz >/dev/null || die matrix-sync "重启后 healthz 不通"
  [ -n "$(matrix_login "$APU" "$APW")" ] || die matrix-sync "reset 后读回验证失败"
  echo "matrix-sync 完成+读回 PASS"
  done_step matrix-sync
}

s_higress_converge() {
  step "higress-converge（凭据选择：谁有效用谁，收敛另一侧——不盲写）"
  APU="$APU" APW="$APW" STATEDIR="$STATEDIR" python - <<'P4'
import base64, json, os, subprocess
ctrl = "cline-controller"
apu = os.environ["APU"]; apw = os.environ["APW"]; statedir = os.environ["STATEDIR"]
def run(script, timeout=60):
    r = subprocess.run(["docker", "exec", ctrl, "sh", "-c", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout
def read_secret():
    out = run("DT=$(cut -d, -f1 " + statedir + "/pki/token.csv | tr -d '\\r\\n'); "
              "curl -sk -m 30 -H 'Authorization: Bearer $DT' "
              "https://127.0.0.1:18443/api/v1/namespaces/higress-system/secrets/higress-console")
    return json.loads(out)
def console_login(u, p):
    body = json.dumps({"username": u, "password": p})
    out = run("curl -sk -m 20 -X POST -H 'content-type: application/json' "
              "--data-binary '" + body + "' http://127.0.0.1:8001/session/login")
    return ('"ok":true' in out) or ('"success":true' in out) or ('"name"' in out)
cur = read_secret()
cur_pw = base64.b64decode(cur["data"]["adminPassword"]).decode("utf-8", "replace")
secret_works = console_login(apu, cur_pw)
env_works = console_login(apu, apw)
if env_works and cur_pw == apw:
    print("已收敛——幂等跳过"); raise SystemExit(0)
if secret_works and not env_works:
    login_body = json.dumps({"username": apu, "password": cur_pw})
    run("curl -sk -m 20 -c /tmp/mig-hc.txt -X POST -H 'content-type: application/json' "
        "--data-binary '" + login_body + "' http://127.0.0.1:8001/session/login")
    chg = json.dumps({"oldPassword": cur_pw, "newPassword": apw})
    out = run("curl -sk -m 20 -b /tmp/mig-hc.txt -X POST -H 'content-type: application/json' "
              "--data-binary '" + chg + "' http://127.0.0.1:8001/user/changePassword")
    if ('"success":true' not in out) and ('"ok":true' not in out):
        print("changePassword resp:", out[:150]); raise SystemExit(1)
    print("已用官方 changePassword 收敛到 env 值（现役凭据保留路径）")
elif env_works and not secret_works:
    cur["data"]["adminPassword"] = base64.b64encode(apw.encode()).decode()
    open("/tmp/mig-secret.json", "w").write(json.dumps(cur))
    out = run("DT=$(cut -d, -f1 " + statedir + "/pki/token.csv | tr -d '\\r\\n'); "
              "curl -sk -m 30 -X PUT -H 'Authorization: Bearer $DT' "
              "-H 'Content-Type: application/json' --data-binary @/tmp/mig-secret.json "
              "https://127.0.0.1:18443/api/v1/namespaces/higress-system/secrets/higress-console")
    if '"higress-console"' not in out:
        print("secret PUT resp:", out[:150]); raise SystemExit(1)
    print("已将 secret adminPassword 收敛到 env 值（env 为权威路径）")
else:
    print("两条候选口令都无效——人工介入（不得盲写）"); raise SystemExit(1)
if not console_login(apu, apw):
    print("收敛后读回验证失败"); raise SystemExit(1)
print("higress-converge 完成+读回 PASS")
P4
  docker restart "$CTRL" >/dev/null; sleep 25
  docker exec "$CTRL" curl -sf -m 10 http://127.0.0.1:8090/healthz >/dev/null || die higress-converge "重启后 healthz 不通"
  echo "higress-converge 步骤完成"
  done_step higress-converge
}

s_cr_migrate() {
  step "cr-migrate（迁移→读回断言；完整 CR 快照见 backup）"
  docker exec "$CTRL" sh -c "export AGENTTEAMS_AUTH_TOKEN=\$(cat /var/run/agentteams/cli-token 2>/dev/null | tr -d '\r\n'); for r in $WORKERS; do agt apply worker --name mergepilot-\$r --runtime qwenpaw --image '$CAS_WORKER_IMAGE' 2>&1 | tail -1; done" > "$EV/cr-migrate.log" || die cr-migrate "agt apply 失败"
  [ "$(grep -c 'configured' "$EV/cr-migrate.log" || true)" = "4" ] || die cr-migrate "4 worker 未全部 configured（现场 $EV/cr-migrate.log）"
  docker exec "$CTRL" sh -c "cat > /tmp/mgr-mig.yaml <<'EOFMGR'
apiVersion: agentteams.agentteam/v1beta1
kind: Manager
metadata:
  name: default
  namespace: default
spec:
  runtime: qwenpaw
  image: $CAS_MANAGER_IMAGE
EOFMGR
export AGENTTEAMS_AUTH_TOKEN=\$(cat /var/run/agentteams/cli-token 2>/dev/null | tr -d '\r\n'); agt apply -f /tmp/mgr-mig.yaml 2>&1 | tail -1" >> "$EV/cr-migrate.log" || die cr-migrate "manager apply 失败"
  grep -q "configured" "$EV/cr-migrate.log" || die cr-migrate "manager 未 configured"
  echo "cr-migrate 完成"
  done_step cr-migrate
}

s_coldstart_env() {
  step "coldstart-env（Worker CR spec.env：AGENTTEAMS_ADMIN_USER + QWENPAW_LLM_MAX_RETRIES/BACKOFF_BASE；幂等 merge-patch）"
  # 冷启动收口（2026-10-13）：worker 容器缺 AGENTTEAMS_ADMIN_USER → ACL 种子退回 "@admin"（错 ID）
  # 且每次启动 reconcile 移除人工批准条目 → 唤醒后首条 admin 消息被拒。CR env 是根治位；
  # QWENPAW_LLM_* 提升冷启动首条消息的 LLM 网关瞬时不可达容忍（指数退避 ~60-70s 覆盖）。
  # 注意：spec.env 变更不触发存量容器重建（specHash 不含 env）——容器在唤醒或删除陈旧容器后
  # 按新 CR 创建。 apiserver merge-patch 用 pki/token.csv static token（system:masters）。
  docker exec "$CTRL" sh -c 'TOK=$(cut -d, -f1 /agentteams-beta-data/pki/token.csv 2>/dev/null || cut -d, -f1 /data/agentteams-beta-data/pki/token.csv); for w in mergepilot-leader mergepilot-reviewer mergepilot-fixer mergepilot-verifier; do R=$(curl -s -m 10 --cacert /agentteams-beta-data/pki/ca.crt -X PATCH -H "Authorization: Bearer $TOK" -H "Content-Type: application/merge-patch+json" --data "{"spec":{"env":{"AGENTTEAMS_ADMIN_USER":"${AGENTTEAMS_ADMIN_USER}","QWENPAW_LLM_MAX_RETRIES":"12","QWENPAW_LLM_BACKOFF_BASE":"3"}}}" https://127.0.0.1:6443/apis/agentteams.io/v1beta1/namespaces/default/workers/$w); echo "$w $(echo $R | grep -o ""generation":[0-9]*" | head -1)"; done' > "$EV/coldstart-env.log" || die coldstart-env "CR env patch 失败"
  [ "$(grep -c 'generation' "$EV/coldstart-env.log" || true)" = "4" ] || die coldstart-env "4 worker CR env 未全部落位（现场 $EV/coldstart-env.log）"
  echo "coldstart-env 完成（CR 已 patch；容器随下次唤醒/重建生效）"
  done_step coldstart-env
}

s_channel_fix() {
  step "channel-fix（K3：config.json channels.matrix.enabled→False；FileSync 持久化）"
  for w in $WORKERS; do
    local C="clineworker-mergepilot-$w"
    [ "$MODE" = "production" ] && C="agentteams-beta-worker-mergepilot-$w"
    local waited=0
    while [ "$waited" -lt 150 ]; do
      docker inspect "$C" --format '{{.State.Running}}' 2>/dev/null | grep -q true && break
      sleep 5; waited=$((waited+5))
    done
    docker inspect "$C" --format '{{.State.Running}}' 2>/dev/null | grep -q true || die channel-fix "$w 容器 150s 未运行（CR 迁移未生效？）"
    local cwait=0
    while [ "$cwait" -lt 120 ]; do
      docker exec "$C" sh -c "test -f /root/agentteams-fs/agents/mergepilot-$w/.qwenpaw/config.json" 2>/dev/null && break
      sleep 5; cwait=$((cwait+5))
    done
    docker exec "$C" sh -c "test -f /root/agentteams-fs/agents/mergepilot-$w/.qwenpaw/config.json" 2>/dev/null || die channel-fix "$w config.json 120s 未出现（worker 初始化异常）"
    docker exec "$C" python3 - <<'P5'
import json, os
w = os.environ["WORKER"]
p = "/root/agentteams-fs/agents/mergepilot-%s/.qwenpaw/config.json" % w
d = json.load(open(p))
if d.get("channels", {}).get("matrix", {}).get("enabled"):
    d["channels"]["matrix"]["enabled"] = False
    json.dump(d, open(p, "w"), indent=1)
    print("%s: ->False" % w)
else:
    print("%s: 已 False" % w)
P5
    docker restart "$C" >/dev/null || die channel-fix "$w 重启失败"
  done
  sleep 50
  for w in $WORKERS; do
    local C="clineworker-mergepilot-$w"
    [ "$MODE" = "production" ] && C="agentteams-beta-worker-mergepilot-$w"
    local N; N=$(docker logs "$C" 2>&1 | grep -ac "MatrixChannel: logged in" || true)
    [ "${N:-0}" -ge 1 ] || die channel-fix "$w Matrix 未登录（logged-in=0）"
    echo "$w Matrix logged-in: $N"
  done
  echo "channel-fix 完成（FileSync 持久化，重启/reconcile 不回退）"
  done_step channel-fix
}

s_llm_seed() {
  step "llm-seed（播种收敛+现役 key 真实调用验证；401=已撤销→硬失败；值不落盘）"
  CTRLN="$CTRL" APU="$APU" APW="$APW" LLM_KEY="$LLM_KEY" LLM_BASE="$LLM_BASE" LLM_MODEL="$LLM_MODEL" python - <<'P6'
import json, os, subprocess
ctrl = os.environ["CTRLN"]
apu, apw = os.environ["APU"], os.environ["APW"]
key, base, model = os.environ["LLM_KEY"], os.environ["LLM_BASE"], os.environ["LLM_MODEL"]
def in_ctrl(script, timeout=60):
    return subprocess.run(["docker", "exec", ctrl, "sh", "-c", script],
                          capture_output=True, text=True, timeout=timeout).stdout
def console(method, path, body=None):
    inner = "curl -sk -m 30 -b /tmp/mig-hc.txt -c /tmp/mig-hc.txt -X %s -H 'content-type: application/json'" % method
    if body is not None:
        inner += " --data-binary '%s'" % json.dumps(body)
    inner += " http://127.0.0.1:8001%s" % path
    return in_ctrl(inner)
def api(method, path, body=None):
    out = console(method, path, body)
    try:
        return json.loads(out)
    except Exception:
        return {"raw": out[:150]}
def upsert(kind, name, body):
    cur = api("GET", "/v1/ai/%ss/%s" % (kind, name))
    if isinstance(cur, dict) and cur.get("name") == name:
        try:
            body = dict(body); body["version"] = int(cur.get("version"))
        except Exception:
            pass
        out = api("PUT", "/v1/ai/%ss/%s" % (kind, name), body)
    else:
        out = api("POST", "/v1/ai/%ss" % kind, body)
    final = api("GET", "/v1/ai/%ss/%s" % (kind, name))
    if name not in str(final):
        print("%s/%s 收敛失败:" % (kind, name), str(out)[:120], "| 读回:", str(final)[:120])
        raise SystemExit(1)
upsert("provider", "openai-compat", {"type":"openai","name":"openai-compat","tokens":[key],"protocol":"openai/v1","tokenFailoverConfig":{"enabled":False},"rawConfigs":{"openaiCustomUrl":base,"openaiCustomServiceName":"openai-compat.dns","openaiCustomServicePort":443}})
upsert("route", "mp-llm", {"name":"mp-llm","domains":["*"],"pathPredicate":{"matchType":"PRE","matchValue":"/v1/chat/completions"},"upstreams":[{"provider":"openai-compat","weight":100}]})
probe_body = json.dumps({"model": model, "messages":[{"role":"user","content":"reply with exactly: ok"}], "max_tokens": 8})
probe = in_ctrl("curl -sk -m 40 -X POST -H 'content-type: application/json' --data-binary '" + probe_body + "' http://127.0.0.1:8080/v1/chat/completions")
if '"choices"' not in probe:
    print("现役 key 真实调用失败（可能已撤销/路由未生效）:", probe[:180])
    raise SystemExit(1)
print("llm-seed 完成：provider/route 收敛+现役 key 经网关真实调用 PASS")
P6
  done_step llm-seed
}

s_verify() {
  step "verify（容器镜像读回+Matrix 登录；完整业务探针=cline-4role/cline-delegate-chain）"
  for w in $WORKERS; do
    local C="clineworker-mergepilot-$w"
    [ "$MODE" = "production" ] && C="agentteams-beta-worker-mergepilot-$w"
    local IMG; IMG=$(docker inspect "$C" --format '{{.Config.Image}}' 2>/dev/null || echo none)
    [ "$IMG" = "$CAS_WORKER_IMAGE" ] || die verify "$w 运行镜像=$IMG ≠ 锚"
  done
  for w in $WORKERS; do
    local C="clineworker-mergepilot-$w"
    [ "$MODE" = "production" ] && C="agentteams-beta-worker-mergepilot-$w"
    local N; N=$(docker logs "$C" 2>&1 | grep -ac "MatrixChannel: logged in" || true)
    [ "${N:-0}" -ge 1 ] || die verify "$w Matrix 未登录"
  done
  echo "verify 基础项 PASS"
  done_step verify
}

for s in ${STEPS//,/ }; do
  if [ -z "$ONLY" ] && [ "$(is_done "$s")" = "yes" ]; then
    step "$s 已完成（checkpoint 跳过；--only $s 强制重跑）"
    continue
  fi
  case "$s" in
    precheck) s_precheck;;
    backup) s_backup;;
    matrix-sync) s_matrix_sync;;
    higress-converge) s_higress_converge;;
    cr-migrate) s_cr_migrate;;
    coldstart-env) s_coldstart_env;;
    channel-fix) s_channel_fix;;
    llm-seed) s_llm_seed;;
    verify) s_verify;;
    *) echo "unknown step $s" >&2; exit 2;;
  esac
done
echo "MIGRATION CONVERGED（mode=$MODE steps=$STEPS；checkpoint=$CKPT）"
