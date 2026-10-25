#!/usr/bin/env bash
# run-fake-gitee.sh — 假 Gitee API 服务【仅隔离验收配置；禁止混入生产配方】。
# 供 deploy/agentteams 验收波使用：console 以 MU_GITEE_API_BASE=http://fake-gitee:19999/api/v5
# 访问；需同时接入执行器网络（ctrl/worker 面）与 console 栈网络（compose 项目网络）。
# 用法：
#   FAKE_GITEE_SOURCE=<fake-gitee.mjs 绝对路径> \
#   AT_NETWORK=<执行器网络名> CONSOLE_NETWORK=<console 栈网络名> \
#   ./run-fake-gitee.sh
set -euo pipefail
SRC="${FAKE_GITEE_SOURCE:?需 FAKE_GITEE_SOURCE（fake-gitee.mjs 绝对路径）}"
AT="${AT_NETWORK:?需 AT_NETWORK（执行器网络名）}"
CN="${CONSOLE_NETWORK:?需 CONSOLE_NETWORK（console 栈网络名）}"
[ -f "$SRC" ] || { echo "FAIL: fake-gitee.mjs 不存在：$SRC" >&2; exit 1; }
docker rm -f fake-gitee >/dev/null 2>&1 || true
docker run -d --name fake-gitee --restart no --network "$AT" \
  -v "$SRC":/fg.mjs:ro node:22-alpine node /fg.mjs >/dev/null
docker network connect "$CN" fake-gitee
docker exec fake-gitee wget -qO- http://127.0.0.1:19999/api/v5/user >/dev/null \
  || { echo "FAIL: fake-gitee 自检失败" >&2; exit 1; }
echo "fake-gitee 就绪（网络：$AT + $CN；仅隔离验收——生命周期随验收波停止）"
