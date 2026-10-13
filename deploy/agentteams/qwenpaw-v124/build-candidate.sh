#!/usr/bin/env bash
# build-candidate.sh — TeamHarness CAS 正式候选镜像构建（qwenpaw-v124 线）
#
# 输入（全部锁定）：
#   SRC_DIR   AgentTeams v1.2.4 源码树（git commit 8765d697，工作区必须 clean）
#   补丁      patches/teamharness-cas-authoritative.patch   （server.py 最终 CAS）
#             patches/controller-canceltask-cas.patch       （Go CancelTask CAS）
#             patches/qwenpaw-worker-coldstart-acl-preseed.patch（冷启动 ACL 预种）
#   依赖      boto3==1.35.99 / botocore==1.35.99（PutObject If-Match 服务模型
#             自 1.35.49 起存在；1.35.0 无此参数，勿回退）
#
# 产出：
#   agentteams-qwenpaw-worker:1.2.4-cas     （worker/leader 运行时）
#   agentteams/agentteams-controller:1.2.4-cas
#   agentteams/agentteams-embedded:1.2.4-cas（ctrl 全家桶替换镜像）
#
# 纪律：任何 apply/构建失败立即退出（set -e），不产出半成品。
# 构建完成后源码树恢复 clean（git checkout），所有修改经由本脚本可复现。
set -euo pipefail

SRC="${SRC_DIR:?SRC_DIR (AgentTeams v1.2.4 源码树) 必需}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PATCH_DIR="$HERE/patches"
WORKER_TAG="agentteams-qwenpaw-worker:1.2.4-cas"
CTRL_TAG="agentteams/agentteams-controller:1.2.4-cas"
EMBEDDED_TAG="agentteams/agentteams-embedded:1.2.4-cas"
UPSTREAM_COMMIT="$(git -C "$SRC" rev-parse HEAD)"
echo "==> upstream source: $UPSTREAM_COMMIT (expect 8765d697...)"

[ -z "$(git -C "$SRC" status --porcelain)" ] || {
  echo "REFUSING: $SRC 工作区不干净"; exit 1; }

# Windows 宿主 checkout 可能带 CRLF；补丁按 LF 生成。归一补丁目标文件后重验 clean。
git -C "$SRC" config core.autocrlf false
for f in plugins/teamharness/mcp/server.py \
         agentteams-controller/internal/server/project_handler.go \
         agentteams-controller/internal/server/project_handler_test.go; do
  rm -f "$SRC/$f" && git -C "$SRC" checkout -- "$f"
done
[ -z "$(git -C "$SRC" status --porcelain)" ] || {
  echo "REFUSING: LF 归一后工作区不干净"; exit 1; }

echo "==> [1/6] 校验并应用补丁（git apply --check 先行）"
git -C "$SRC" apply --check "$PATCH_DIR/teamharness-cas-authoritative.patch"
git -C "$SRC" apply --check "$PATCH_DIR/controller-canceltask-cas.patch"
git -C "$SRC" apply --check "$PATCH_DIR/qwenpaw-worker-coldstart-acl-preseed.patch"
git -C "$SRC" apply "$PATCH_DIR/teamharness-cas-authoritative.patch"
git -C "$SRC" apply "$PATCH_DIR/controller-canceltask-cas.patch"
git -C "$SRC" apply "$PATCH_DIR/qwenpaw-worker-coldstart-acl-preseed.patch"
python -m py_compile "$SRC/plugins/teamharness/mcp/server.py"
echo "    patches applied + py_compile OK"

echo "==> [2/6] 构建插件适配 zip（ruby 脚本的 python 等价复刻）"
python "$HERE/build-qwenpaw-plugin.py" \
  --plugin-root "$SRC/plugins/teamharness" \
  --out-dir "$SRC/dist/adapters/qwenpaw"
python "$HERE/build-qwenpaw-plugin.py" \
  --plugin-root "$SRC/plugins/workerflow" \
  --out-dir "$SRC/dist/adapters/qwenpaw"
ls -la "$SRC/dist/adapters/qwenpaw/"

echo "==> [3/6] qwenpaw Dockerfile 注入锁定依赖（幂等锚定）"
python "$HERE/patch-qwenpaw-dockerfile.py" --dockerfile "$SRC/qwenpaw/Dockerfile"

echo "==> [4/6] 构建 worker 镜像 $WORKER_TAG"
# 网络受限环境可覆盖；空值（APT_MIRROR=）= 用上游默认源。
# 注意值是"主机名"不是路径——Dockerfile sed 只替换 deb.debian.org，/debian 路径保留。
MIRROR_ARGS=()
if [ -n "${APT_MIRROR-}" ]; then MIRROR_ARGS+=(--build-arg "APT_MIRROR=$APT_MIRROR"); fi
if [ -n "${PIP_INDEX_URL-}" ]; then MIRROR_ARGS+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL"); fi
if [ -n "${NPM_REGISTRY-}" ]; then MIRROR_ARGS+=(--build-arg "NPM_REGISTRY=$NPM_REGISTRY"); fi
docker build --build-context shared="$SRC/shared/lib" \
  "${MIRROR_ARGS[@]}" \
  -f "$SRC/qwenpaw/Dockerfile" -t "$WORKER_TAG" "$SRC"

echo "==> [5/6] 构建 controller 镜像 $CTRL_TAG"
# 与上游 Makefile build-agentteams-controller 同形：context=agentteams-controller/，
# 构建前把 manager/agent 与 plugins/ 拷入，构建后移除。
cp -r "$SRC/manager/agent" "$SRC/agentteams-controller/agent"
cp -r "$SRC/plugins" "$SRC/agentteams-controller/plugins"
docker build -f "$SRC/agentteams-controller/Dockerfile" -t "$CTRL_TAG" "$SRC/agentteams-controller/"
rm -rf "$SRC/agentteams-controller/agent" "$SRC/agentteams-controller/plugins"

echo "==> [6/6] 构建 embedded 镜像 $EMBEDDED_TAG"
docker build -f "$SRC/agentteams-controller/Dockerfile.embedded" \
  --build-arg AGENTTEAMS_CONTROLLER_IMAGE="$CTRL_TAG" \
  -t "$EMBEDDED_TAG" "$SRC"

echo "==> 记录镜像 digest"
for t in "$WORKER_TAG" "$CTRL_TAG" "$EMBEDDED_TAG"; do
  docker image inspect "$t" --format '{{.RepoTags}} index-digest={{.RepoDigests}} id={{.Id}}'
done

echo "==> 恢复源码树 clean（修改全部经由本脚本+补丁重现）"
git -C "$SRC" checkout -- . && git -C "$SRC" clean -fdq dist/ 2>/dev/null || true
git -C "$SRC" status --porcelain | head -3

echo "BUILD COMPLETE"
echo "  worker   : $WORKER_TAG"
echo "  ctrl     : $CTRL_TAG"
echo "  embedded : $EMBEDDED_TAG"
