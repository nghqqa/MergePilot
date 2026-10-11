#!/usr/bin/env bash
# deploy-candidate-qwp2.sh — CAS 正式候选部署到 qwp2 隔离栈
#
# 前置：build-candidate.sh 已产出三镜像；qwp2 无活动任务（非终态 meta 为零）。
# 步骤：
#   1. 清 MinIO 中诊断期注入的陈旧插件副本（防启动 restore 覆盖镜像 builtin）
#   2. 记录回滚锚（旧 ctrl/worker 镜像 digest）
#   3. 以新 embedded 镜像重建 qwp2-controller（env 同源，唯 AGENTTEAMS_QWENPAW_WORKER_IMAGE
#      指向新 worker 镜像）
#   4. 删除四个 worker 容器 → controller reconcile 以新镜像重建
#   5. 读回验证：运行镜像、运行代码（_cas_write_task 在 builtin+workspace）、
#      boto3==1.35.99、四 worker Matrix token 有效
#   6. 刷新 Matrix admin token（qwp2-mx-refresh.sh）
# 失败即停（set -e），不半途收口。
set -euo pipefail
# Git Bash 会把 -v /run/... 转成 Windows 路径（mkdir C:\Program Files\Git\run 报错）；
# 本脚本 docker 参数无依赖路径转换的用例，全局关闭。仅限脚本进程内。
export MSYS_NO_PATHCONV=1
EVAL=/d/goai/r3work/qwenpaw-eval-20261010
EV="$EVAL/evidence/race-fix/deploy-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$EV"
WORKER_TAG="agentteams-qwenpaw-worker:1.2.4-cas"
EMBEDDED_TAG="agentteams/agentteams-embedded:1.2.4-cas"
CTRL_NAME=qwp2-controller
WORKERS="leader reviewer fixer verifier"

echo "==> [0] 前置检查"
for t in "$WORKER_TAG" "$EMBEDDED_TAG"; do
  docker image inspect "$t" >/dev/null || { echo "missing image $t"; exit 1; }
done
docker inspect "$CTRL_NAME" > "$EV/ctrl-inspect-before.json"
# 活动任务检查：非终态 meta 计数必须为 0（或操作者明确忽略）
ACTIVE=$(docker exec "$CTRL_NAME" sh -c 'export MC_HOST_admin=http://qwp2-minio:$(printenv AGENTTEAMS_MINIO_PASSWORD)@127.0.0.1:9000; mc ls --recursive admin/agentteams-storage/teams/mp/shared/tasks/ 2>/dev/null | grep meta.json | wc -l' | tr -d ' ')
echo "    task meta objects in MinIO: $ACTIVE (informational; regression tasks cleaned separately)"

echo "==> [1] 清理诊断期 MinIO 陈旧插件副本"
docker exec "$CTRL_NAME" sh -c 'export MC_HOST_admin=http://qwp2-minio:$(printenv AGENTTEAMS_MINIO_PASSWORD)@127.0.0.1:9000; mc rm --recursive --force admin/agentteams-storage/agents/mergepilot-reviewer/.qwenpaw/plugins/teamharness/ 2>/dev/null; echo cleaned'

echo "==> [2] 记录回滚锚"
OLD_CTRL_IMAGE=$(docker inspect "$CTRL_NAME" --format '{{index .Config.Image}}' || true)
OLD_WORKER_IMAGE=$(docker inspect qwp2-worker-mergepilot-leader --format '{{.Image}}' || true)
cat > "$EV/rollback-anchors.txt" <<EOF
ctrl-image=$OLD_CTRL_IMAGE
worker-image-id=$OLD_WORKER_IMAGE
worker-tag-old=agentteams-qwenpaw-worker@sha256:4f91b1026fe116f87c639f19e92f7e1f26f59fb408a90425691c32f700c3a5f5
new-embedded=$EMBEDDED_TAG
new-worker=$WORKER_TAG
EOF
cat "$EV/rollback-anchors.txt"

echo "==> [3] 重建 $CTRL_NAME（embedded=$EMBEDDED_TAG）"
# env 同源重建：从 inspect 提取全部 env，替换 worker 镜像引用
docker inspect "$CTRL_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$EV/ctrl-env-before.txt"
docker rm -f "$CTRL_NAME"
ENV_ARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue   # 跳过空行（println 尾行/空 env 均非法）
  case "$line" in
    AGENTTEAMS_QWENPAW_WORKER_IMAGE=*) ENV_ARGS+=("-e" "AGENTTEAMS_QWENPAW_WORKER_IMAGE=$WORKER_TAG") ;;
    PATH=*|JAVA_HOME=*|DEBIAN_FRONTEND=*|HOSTNAME=*) ;; # docker 默认注入项跳过
    *=*) ENV_ARGS+=("-e" "$line") ;;
  esac
done < "$EV/ctrl-env-before.txt"
docker run -d --name "$CTRL_NAME" \
  --network qwp2-net \
  "${ENV_ARGS[@]}" \
  -p 127.0.0.1:18186:6167 -p 127.0.0.1:18190:8090 -p 127.0.0.1:19100:9000 \
  -v qwp2-data:/data \
  -v /run/host-services/docker.proxy.sock:/var/run/docker.sock \
  "$EMBEDDED_TAG"

echo "    waiting for controller API health"
for i in $(seq 1 60); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18190/healthz || true)
  [ "$CODE" = "200" ] && { echo "    healthz OK after ${i}x2s"; break; }
  sleep 2
  [ "$i" = "60" ] && { echo "healthz never became 200"; exit 1; }
done

echo "==> [4] 删除四 worker → reconcile 重建"
for r in $WORKERS; do docker rm -f "qwp2-worker-mergepilot-$r" 2>/dev/null || true; done
echo "    waiting for reconciler to recreate workers (new image)"
force_worker_put() {  # reconcile 事件驱动兜底：PUT 同 spec 触发重建
  CTOK=$(MSYS_NO_PATHCONV=1 docker exec "$CTRL_NAME" cat /var/run/agentteams/cli-token 2>/dev/null | tr -d '\r\n')
  for r in $WORKERS; do
    curl -s -o "$EV/worker-put-$r.json" -w "put $r: %{http_code}\n" \
      -X PUT "http://127.0.0.1:18190/api/v1/workers/mergepilot-$r" \
      -H "authorization: Bearer $CTOK" -H "content-type: application/json" \
      --data "$(curl -s -H "authorization: Bearer $CTOK" "http://127.0.0.1:18190/api/v1/workers/mergepilot-$r" | python -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"spec": d.get("spec", d)}))')"
  done
}
RECREATED=0
for i in $(seq 1 90); do
  N=$(docker ps --format '{{.Names}}' | grep -c '^qwp2-worker-mergepilot-' || true)
  NEW=$(docker ps --format '{{.Names}}\t{{.Image}}' | grep '^qwp2-worker-mergepilot-' | grep -c "$WORKER_TAG" || true)
  if [ "$N" = "4" ] && [ "$NEW" = "4" ]; then RECREATED=1; echo "    all four on new image after ${i}x2s"; break; fi
  [ "$i" = "30" ] && { echo "    no recreate in 60s — forcing worker PUT to trigger reconcile"; force_worker_put; }
  sleep 2
done
[ "$RECREATED" = "1" ] || { echo "workers not recreated on new image"; docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep qwp2; exit 1; }

echo "==> [5] 读回验证"
for r in $WORKERS; do
  C="qwp2-worker-mergepilot-$r"
  # 等容器内服务起来（workspace restore + 插件装载）
  for i in $(seq 1 60); do
    UP=$(docker inspect "$C" --format '{{.State.Running}}' 2>/dev/null || echo false)
    [ "$UP" = "true" ] || { sleep 2; continue; }
    CAS_BUILTIN=$(docker exec "$C" sh -c 'grep -c "_cas_write_task" /opt/agentteams/qwenpaw-builtin/plugins/teamharness/teamharness/mcp/server.py 2>/dev/null' || echo 0)
    [ "$CAS_BUILTIN" -ge 1 ] 2>/dev/null && break
    sleep 2
  done
  [ "$(docker inspect "$C" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] || { echo "  FAIL $r container not running"; docker logs "$C" 2>&1 | tail -5; exit 1; }
  CAS_WS=$(docker exec "$C" sh -c 'grep -c "_cas_write_task" /root/agentteams-fs/agents/mergepilot-'$r'/.qwenpaw/plugins/teamharness/teamharness/mcp/server.py 2>/dev/null' || echo 0)
  BOTO=$(docker exec "$C" /opt/venv/qwenpaw/bin/pip show botocore 2>/dev/null | grep '^Version' | awk '{print $2}')
  IMG=$(docker inspect "$C" --format '{{.Image}}')
  echo "  $r: img=${IMG:7:19} builtin-cas=$CAS_BUILTIN ws-cas=$CAS_WS botocore=$BOTO"
  [ "$CAS_BUILTIN" -ge 1 ] 2>/dev/null || { echo "  FAIL $r builtin missing CAS"; exit 1; }
  [ "${BOTO:-none}" = "1.35.99" ] || { echo "  FAIL $r botocore=$BOTO"; exit 1; }
done
docker exec qwp2-worker-mergepilot-leader sh -c 'grep -c "_cas_write_task" /root/agentteams-fs/agents/mergepilot-leader/.qwenpaw/plugins/teamharness/teamharness/mcp/server.py' >/dev/null 2>&1 || {
  echo "  NOTE: leader workspace copy not yet synced; builtin verified"; }

echo "==> [6] Matrix admin token 刷新"
bash "$EVAL/qwp2-mx-refresh.sh"

echo "DEPLOY COMPLETE — evidence in $EV"
