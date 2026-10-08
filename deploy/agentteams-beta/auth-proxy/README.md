# AgentTeams 认证代理（auth-proxy）— 运维说明

2026-10-08 随泄露 token 处置落地。本目录提供：nginx 配置模板、部署器、ctrl 重建后
注册表恢复工具。父级手册见 [`../RUNBOOK.md`](../RUNBOOK.md)（部署/健康/故障/备份）。

## 1. 链路

```
MergePilot Console (beta-mp-console)
      │  MU_AGENTTEAMS_BASE_URL=http://at-auth-proxy:8091
      │  MU_AGENTTEAMS_TOKEN=<入口凭据，非 ctrl token>
      ▼
at-auth-proxy（nginx，agentteams-beta_atnet；回环运维口 127.0.0.1:28565→8091）
      │  入口精确匹配新凭据；命中后以 ctrl 当前 cli-token 改写 Authorization
      ▼
agentteams-beta-ctrl:8090（controller API；宿主端口 28562/28563 已移除——唯一受控入口即本代理）
      ▼
四具名 worker（mergepilot-{leader,reviewer,fixer,verifier}，共享 ctrl netns）
```

安全语义：入口凭据只认证"谁能用代理"；ctrl token 只存在于代理↔ctrl 一跳。旧/泄露
token、无凭据、伪造凭据一律 401，且**不可能借道代理**（Authorization 恒被改写）。

## 2. 配置真源与秘密位置（全部不入库）

| 项 | 位置 | 说明 |
|---|---|---|
| Console 活体 env 真源 | 运维区 `r3work/rc18-prod/inspect/console-rc18-agentteams-rotated-inspect.json`（swap 输入）与 `console-rc18-agentteams-rotated-live.json`（换后活体快照） | 本地受保护目录；仓库永不收录 |
| 入口凭据 | 运维区 `prod-data/secrets/agentteams-auth-proxy-credential.txt` | 64-hex；仅部署器读取 |
| 代理渲染配置 | `prod-data/secrets/agentteams-auth-proxy/default.conf`（生产）；本目录 `rendered/`（默认输出，已 gitignore） | 含真实凭据，0600 |
| ctrl cli-token | 仅 ctrl 容器 `/var/run/agentteams/cli-token` | 部署时由部署器现读，不落盘 |

## 3. 网络与端口

- 网络：`agentteams-beta_atnet`（必须已存在；部署器 fail-closed 拒绝建网）。
- 代理内部监听 8091（atnet 内）；回环运维发布 `127.0.0.1:28565→8091`（凭据把守）。
- **ctrl 自身零宿主发布**：28562（API）/28563（Matrix）已于 2026-10-08 移除。
  禁止以任何形式复开 ctrl 宿主端口；Matrix(6167) 由 Console 经 atnet 内部直连
  （`MU_AGENTTEAMS_MATRIX_URL=http://agentteams-beta-ctrl:6167`），不经代理。

## 4. 启动顺序（全新拉起/迁移）

1. `agentteams-beta` 栈 up（pg、ctrl；compose 见 `../docker-compose.yml`，卷=数据真源）。
2. ctrl healthy 后部署代理（nginx 启动时要能解析 ctrl DNS）：
   `node deploy/agentteams-beta/auth-proxy/deploy-auth-proxy.cjs --entry-credential-file <秘密文件>`
3. worker 注册：见 §6 恢复矩阵（新栈走 `../provision-workers.sh`；ctrl 重建后走 `recover-agentteams.mjs`）。
4. Console 最后启动，env 指向代理（`MU_AGENTTEAMS_BASE_URL=http://at-auth-proxy:8091` +
   `MU_AGENTTEAMS_TOKEN=<入口凭据>`；用 scripts/swap-console.cjs 继承式换版）。

## 5. 健康验收（全部通过才算恢复/部署完成）

```bash
CRED=$(cat <入口凭据文件>)                                   # 值不回显
P=127.0.0.1:28565
# 四向矩阵：200 / 401 / 401 / 401（伪造值用 16+ 位假串即可）
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CRED" http://$P/api/v1/workers
curl -s -o /dev/null -w '%{http_code}\n' http://$P/api/v1/workers                       # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer definitely-fake-value" http://$P/api/v1/workers  # 401
# 全链（Console 容器视角）+ 就绪数
docker exec beta-mp-console node -e "const b=process.env.MU_AGENTTEAMS_BASE_URL,t=process.env.MU_AGENTTEAMS_TOKEN;fetch(b+'/api/v1/workers',{headers:{authorization:'Bearer '+t}}).then(r=>r.json()).then(j=>console.log('workers:',j.total))"
# 期望 workers: 4（且 GET /api/mu/agentteams-status → runtime_state: active, workers_ready: 4）
# Matrix 旁路核验（不经代理）：docker exec beta-mp-console node -e "<_matrix/login 探针>" → 200
# Console：docker inspect beta-mp-console --format '{{.State.Health.Status}}' → healthy
```

## 6. 关键差异：`docker restart` vs 容器【重建】（rm+run / compose up 重建）

| | restart | 重建 |
|---|---|---|
| 容器文件系统 | 保留 | **新建** |
| cli-token（在容器 FS，非卷） | 存活，旧 token 继续有效 | **再生成 → 全部旧 token 立即 401** |
| 注册表 workers/teams/humans（在容器 FS） | 存活 | **清空**：worker 端 `agt get` 404，循环 `Worker config not ready` |
| `/data` 卷（worker-creds、MinIO、Matrix 数据） | 保留 | 保留（**注册表不在卷里，卷救不了注册表**） |
| 恢复工具 | `recover-runtime.sh`（RUNBOOK §3/§4） | **`recover-agentteams.mjs`（本目录）** |
| 恢复完成判定 | 心跳/桥探针过 | **ctrl 列表 4/4 worker Running（§5 验收）**，不是"token 换成功" |

> 撤销语义：**需要让历史 token 全部失效（含疑似泄露）时，重建 ctrl 即撤销**——这是
> 2026-10-08 实证的机制（token 文件非卷 + 校验为容器 FS 内文件精确匹配）。重建后
> 必须：代理上游换新 cli-token（重跑部署器）→ Console env 指向代理（不变则只换凭据值）
> → 跑恢复工具 → §5 全验收。

## 7. 恢复操作

```bash
# ctrl 重建后（本轮实证场景）：幂等，重复运行不产生重复注册
node deploy/agentteams-beta/auth-proxy/recover-agentteams.mjs --container beta-mp-console
# 输出单行 JSON：{"ok":true,"created":[...],"workers":[...],"workerCount":4,...}；非零退出=失败
```

- 恢复路径=Console 自带 `ensureFourAgents`（`console/backend/lib/multiuser/agents/agentteams-executor.mjs`），
  与 fix-orchestrator 每次 run 的自愈调用同源——不旁路业务契约，不手写 POST。
- 幂等由契约保证（GET 判存在→缺者 POST/在者 PUT，409/404 感知→完整性复查），
  测试锁定于 `console/backend/test/agentteams-recovery.test.mjs`。
- 故障形态速查：`healthy` 阶段失败=代理链路/凭据问题（先查 §5 前两项）；
  `ensure` 阶段失败=controller 侧（看 ctrl 日志）；`verify` 阶段失败=复查不到 4/4。
- controller 重启（非重建）后的 netns/桥恢复仍走 `../recover-runtime.sh`。

## 8. 安全红线（恢复/重建方案的设计约束）

- 不得重新启用已泄露凭据：旧 token 已于 2026-10-08 随 ctrl 重建失效（直连 401 实证），
  任何"恢复"都不应把它写回任何 env/配置。
- 不得悄悄复开旧入口：ctrl 宿主端口保持零发布；代理是唯一受控入口。
- 入口凭据/上游 token 只存在于 §2 所列位置；渲染配置目录必须在 .gitignore 覆盖内。
- 代理配置/部署器输出一律脱敏（sha256 前 16 位）。

## 9. 凭据轮换的候选路径（未实测，不作标准流程）

上游 `agt rotate`（AgentTeams CLI 内置 rotate credentials）是未来免重建轮换的候选方式。
**在隔离环境实测验证前，不将其写入标准轮换流程**；当前经验证的轮换/撤销路径仍为 §6 的
重建语义（代价=注册表恢复一次）。

## 10. 供应链

| 镜像 | 钉扎 | 来源 |
|---|---|---|
| nginx（代理） | `nginx:alpine@sha256:df221db836e1754089190208cee7eeda94f233197056426eda74a43ab1abeac2` | docker hub 官方 alpine（2026-10-08 部署所取 digest） |

换镜像时：更新 `deploy-auth-proxy.cjs` `DEFAULT_IMAGE` 与本表，四向矩阵复验。

## 11. 残余待办（非阻断）

- `.zcode` 历史会话转录中仍留有本轮之前各代已失效/已轮换的凭据副本；转录清理依赖
  产品侧删除能力，作为独立清理待办跟踪，不阻塞任何运行面。
