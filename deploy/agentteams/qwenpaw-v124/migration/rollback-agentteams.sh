#!/usr/bin/env bash
# rollback-agentteams.sh — CAS 迁移回滚（完整 CR spec/secret/CM 还原 + 旧栈进程切回 + 业务验证）
#
# 前置：migrate-agentteams.sh --steps backup 已执行（$EV 有完整 CR/secret/CM/kine 快照）。
# 语义：
#   1. 经【新 ctrl CLI（agt apply）】恢复完整 CR 对象（含 runtime/image/身份绑定——不留残留）
#   2. 还原 higress secret/configmap 原值（file-store apiserver，static token）
#   3. 停新 ctrl → 起 223ddc2（同数据卷+同状态目录+原 env——K4：同目录沿用旧 pki/JWT）
#   4. 验证：healthz/0 FATAL/Matrix admin 登录/Matrix 业务探针真实回复
#      （223ddc2 API 通道重启后为 K4 黑盒——回滚验证走 Matrix/MinIO 通道，如实记录）
set -euo pipefail
export MSYS_NO_PATHCONV=1

NEW_CTRL="${NEW_CTRL:?新 ctrl 容器名（当前运行的 CAS ctrl）}"
EV="${MIGRATE_EV:?MIGRATE_EV（migrate 快照目录）必需}"
STATEDIR="${STATEDIR:-/data/ctrl-statedir}"
OLD_IMAGE="${OLD_IMAGE:-agentteams/agentteams-embedded:223ddc2}"
DATAVOL="${DATAVOL:-cline-data}"
NET="${NET:-cline-net}"
APW="${APW:?APW（admin 口令，值不回显）必需}"
APU="${APU:?APU 必需}"
MDOM="${MDOM:?MDOM 必需}"
MX_PORT="${MX_PORT:-18196}"
OLD_API_PORT="${OLD_API_PORT:-18095}"
PREFIX="${PREFIX:-cline}"
ROLLBACK_PROBE_ROOM="${ROLLBACK_PROBE_ROOM:?ROLLBACK_PROBE_ROOM（回滚后业务探针房间，URL 编码）必需}"
WORKERS="leader reviewer fixer verifier"
step() { echo "==> [$(date -u '+%F %T')] $*"; }
die() { echo "FATAL[rollback]: $*" >&2; exit 1; }

step "1 完整 CR 还原（agt apply -f 快照整对象——含 runtime/image/身份绑定）"
for w in $WORKERS; do
  [ -f "$EV/cr-worker-$w-before.json" ] || die "缺 $EV/cr-worker-$w-before.json"
  docker cp "$EV/cr-worker-$w-before.json" "$NEW_CTRL:/tmp/mig-rb-worker-$w.json" || die "CR 快照拷入失败 $w"
  docker exec "$NEW_CTRL" sh -c 'export AGENTTEAMS_AUTH_TOKEN=$(cat /var/run/agentteams/cli-token); agt apply -f /tmp/mig-rb-worker-'"$w"'.json 2>&1 | tail -1' || die "worker $w CR 还原失败"
done
[ -f "$EV/cr-manager-before.json" ] || die "缺 manager CR 快照"
docker cp "$EV/cr-manager-before.json" "$NEW_CTRL:/tmp/mig-rb-mgr.json" || die "manager 快照拷入失败"
docker exec "$NEW_CTRL" sh -c 'export AGENTTEAMS_AUTH_TOKEN=$(cat /var/run/agentteams/cli-token); agt apply -f /tmp/mig-rb-mgr.json 2>&1 | tail -1' || die "manager CR 还原失败"
echo "CR 还原完成（整对象）"

step "2 secret/CM 还原"
[ -f "$EV/secret-before.json" ] || die "缺 secret 快照"
docker cp "$EV/secret-before.json" "$NEW_CTRL:/tmp/mig-rb-secret.json" || die "secret 拷入失败"
DT=$(docker exec "$NEW_CTRL" sh -c "cut -d, -f1 $STATEDIR/pki/token.csv | tr -d '\r\n'")
R=$(docker exec "$NEW_CTRL" sh -c "curl -sk -m 30 -X PUT -H 'Authorization: Bearer $DT' -H 'Content-Type: application/json' --data-binary @/tmp/mig-rb-secret.json https://127.0.0.1:18443/api/v1/namespaces/higress-system/secrets/higress-console")
echo "$R" | grep -q 'higress-console' || die "secret 还原失败: $(echo "$R" | head -c 120)"
if [ -f "$EV/cm-before.json" ]; then
  docker cp "$EV/cm-before.json" "$NEW_CTRL:/tmp/mig-rb-cm.json" || die "cm 拷入失败"
  R=$(docker exec "$NEW_CTRL" sh -c "curl -sk -m 30 -X PUT -H 'Authorization: Bearer $DT' -H 'Content-Type: application/json' --data-binary @/tmp/mig-rb-cm.json https://127.0.0.1:18443/api/v1/namespaces/higress-system/configmaps/higress-console")
  echo "$R" | grep -q 'higress-console' || echo "WARN: cm 还原响应异常（非阻断）: $(echo "$R" | head -c 100)"
fi
echo "secret/CM 还原完成"

step "3 切回旧栈（同卷+同状态目录+原 env——K4：沿用旧 pki/JWT）"
OLD_CTRL="${OLD_CTRL:-cline-controller}"
MODE="${MODE:-copy}"
if [ "$MODE" = "production" ]; then
  echo "production 回滚切回须按生产维护方案 §4 的 beta-ctrl 重建命令执行（资源一致性由方案保证），本工具不代跑"
  exit 0
fi
docker rm -f "$NEW_CTRL" >/dev/null 2>&1
ENV_ARGS=()
while IFS= read -r line; do
  line="${line%$''}"
  [ -z "$line" ] && continue
  case "$line" in
    PATH=*|JAVA_HOME=*|DEBIAN_FRONTEND=*|HOSTNAME=*|HOME=*|AGENTTEAMS_MATRIX_URL=*|AGENTTEAMS_DATA_DIR=*|AGENTTEAMS_CONTROLLER_URL=*|AGENTTEAMS_DOCKER_NETWORK=*|AGENTTEAMS_FS_ENDPOINT=*) ;;
    *=*) ENV_ARGS+=("-e" "$line");;
  esac
done < <(cat "$EV/old-ctrl-env.txt" 2>/dev/null || echo "")
docker run -d --name "$OLD_CTRL" --network "$NET" --network-alias agentteams-beta-controller "${ENV_ARGS[@]}" \
  -e AGENTTEAMS_DATA_DIR="$STATEDIR" \
  -e AGENTTEAMS_MATRIX_URL="http://$OLD_CTRL:6167" \
  -e AGENTTEAMS_CONTROLLER_URL="http://$OLD_CTRL:8090" \
  -e AGENTTEAMS_DOCKER_NETWORK="$NET" \
  -e AGENTTEAMS_FS_ENDPOINT="http://$OLD_CTRL:9000" \
  -e AGENTTEAMS_RESOURCE_PREFIX="$PREFIX-" \
  -p 127.0.0.1:"$OLD_API_PORT":8090 -p 127.0.0.1:"$MX_PORT":6167 -p 127.0.0.1:18097:9000 -p 127.0.0.1:18098:8001 \
  -v "$DATAVOL:/data" \
  -v /run/host-services/docker.proxy.sock:/var/run/docker.sock \
  "$OLD_IMAGE" >/dev/null
sleep 20
OK=0
for i in $(seq 1 45); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$OLD_API_PORT/healthz" 2>/dev/null || true)
  [ "$CODE" = "200" ] && { echo "旧栈 healthz OK"; OK=1; break; }
  sleep 2
done
[ "$OK" = "1" ] || die "旧栈 healthz 45 次未通"
FAILS=$(docker logs "$OLD_CTRL" 2>&1 | grep -cE "FATAL|gave up" || true)
[ "$FAILS" = "0" ] || die "旧栈 FATAL 计数=$FAILS"

step "4 旧栈业务验证（Matrix 通道：admin 登录+worker 探针真实回复）"
MTOK=$(curl -s -m 15 -X POST -H 'content-type: application/json' \
  -d "{\"type\":\"m.login.password\",\"user\":\"$APU\",\"password\":\"$APW\"}" \
  "http://127.0.0.1:$MX_PORT/_matrix/client/v3/login" | python -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')
[ -n "$MTOK" ] || die "旧栈 Matrix admin 登录失败"
echo "Matrix admin 登录 PASS"
SUB="rb-verify-$(date +%s)"
BODY=$(printf '{"msgtype":"m.text","body":"@mergepilot-reviewer:agentteams-beta-matrix:6167 [rb:%s]\\nReply with exactly: [rb:%s] then one JSON {\\"verdict\\":\\"...\\"}"}' "$SUB" "$SUB")
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "http://127.0.0.1:$MX_PORT/_matrix/client/v3/rooms/$ROLLBACK_PROBE_ROOM/send/m.room.message/$SUB" \
  -H "Authorization: Bearer $MTOK" -H "content-type: application/json" --data-raw "$BODY")
[ "$CODE" = "200" ] || die "探针发送失败 code=$CODE"
sleep 75
HIT=$(curl -s "http://127.0.0.1:$MX_PORT/_matrix/client/v3/rooms/$ROLLBACK_PROBE_ROOM/messages?dir=b&limit=10" -H "Authorization: Bearer $MTOK" | python -c '
import json,sys
d=json.load(sys.stdin)
sub=sys.argv[1]
hit=[e["content"]["body"][:120] for e in d.get("chunk",[]) if e.get("type")=="m.room.message" and ("[rb:%s]"%sub) in str(e["content"].get("body","")) and e["sender"].startswith("@mergepilot-reviewer")]
print(hit[0] if hit else "NONE")' "$SUB")
[ "$HIT" != "NONE" ] || die "回滚后业务探针无回复（Matrix 通道未恢复）"
echo "回滚业务探针回复: $HIT"
echo "ROLLBACK CONVERGED（数据对账由调用方以 MinIO/kine 快照比对；API 通道=K4 黑盒如实记录）"
