# AgentTeams 执行器部署真源（MANIFEST）

- 日期：2026-10-10（v1；rc.24 轮建立——生产角色覆盖事故与 LLM 断路定案后）
- 性质：**受版本控制的部署唯一真源**。凭据只以环境变量引用出现，版本库零密钥。
- 执行入口：`recover-workers.sh`（同目录）——本清单描述的期望状态的唯一施加工具，幂等可重跑，步骤顺序即契约。

## 1. 组件与镜像（digest 锁定）

| 组件 | 镜像 | digest | 说明 |
|---|---|---|---|
| controller（含 Matrix/MinIO/supervisord 全家桶） | `agentteams/agentteams-embedded:223ddc2` | `sha256:ae47995d209f…`（2026-09-29 供应链裁决锁定） | 上游 embedded；重启即数据卷内状态保留 |
| worker ×4（leader/reviewer/fixer/verifier） | `ghcr.io/nghqqa/copaw-worker:223ddc2-v5fix-get-soul-shim` | `sha256:691cf6fb0773de78087be5ee77bcb4e4c1468fde139810db70f57ac61ed67e36` | **本仓 shim 构建**（基于上游 `agentteams/copaw-worker:223ddc2-agentloop-v5fix` @ `sha256:4be8eccfe4fb…` 追加 FileSync.get_soul/get_agents_md 兼容 shim，standard+lite 双 venv） |
| manager | `agentteams/manager:223ddc2`（ctrl env 内嵌引用） | 上游 223ddc2 线 | 由 ctrl 管理 |

- worker 容器形态：`--network container:<ctrl>`（共享 ctrl netns——上游 embedded 给 worker 注入 127.0.0.1 URL 的约束）+ auth 卷挂载 + `--restart unless-stopped`。
- shim 镜像构建产物：`prod-data/rc24-switch/Dockerfile.worker-shim` + `sync-shim.txt`（cat 追加式，构建确定性）；**回滚** = 恢复上游原 tag `agentteams/copaw-worker:223ddc2-agentloop-v5fix` 并重跑 recover-workers.sh（代价=get_soul 缺陷回归）。

## 2. 配置三源分工（谁生成/谁覆盖/谁加载）

| 层 | 生成者 | 覆盖时机 | 加载者 | 内容 |
|---|---|---|---|---|
| **ctrl 模板** | controller（worker spec → openclaw.json 模板） | **每次 reconcile**（Sleeping→Running touch、worker 重建、spec 变更） | 写入 MinIO `agents/<name>/openclaw.json` | 默认 provider=agentteams-gateway（指向 ctrl :8080）；**不含 deepseek-direct** |
| **MinIO 配置** | ① ctrl reconcile 写入 ② recover-workers.sh 步骤 3 追加 deepseek-direct（reconcile 之后） | ①会抹掉②的模型段（顺序契约的根本原因） | worker 启动 mirror（MinIO→本地全量）+ **运行时轮询**（"Config changed, re-bridging"，约 60s 周期，无需重启） | 最终期望态：`models.providers.deepseek-direct` + `agents.defaults.model.primary=deepseek-direct/deepseek-chat` |
| **运行进程实际配置** | worker 本地 mirror/重桥写入 `/root/.copaw-worker/<name>/openclaw.json` | mirror（启动）+ re-bridge（运行时轮询到 MinIO 变化） | copaw agent 逐查询读取 | recover-workers.sh 步骤 4/5 验证的终态 |

**铁律**：任何对 MinIO openclaw.json 的模型段修改，必须发生在**最后一次 reconcile 之后**；`recover-workers.sh` 是该顺序的唯一执行者——人工直接改 JSON 会被下一次 reconcile 抹掉（2026-10-10 生产事故实证）。

**⚠️ 2026-10-10 itest 隔离定案（S1/S2 实测）——本栈无 durable 模型配置路径**：
- sync_loop 合并规则="models 从远端替换；agents（含 primary）保留本地"→ 改 primary 不传播；
- 正确 patch 形态=重定向 `agentteams-gateway` provider 的 baseUrl/apiKey（models 段随远端替换，60s 周期传播）；
- 但 ctrl 存在**周期性模板重推**（与 reconcile 无关的异步行为），最终把 MinIO 打回 gateway(8080)——工具收敛后往返 PASS（S1，5s 真实 LLM），重推落盘后往返 FAIL（S2，MODEL 422）；
- worker spec 无 provider 覆盖位，模板硬编码 broken gateway。
- **结论**：`recover-workers.sh` = 可重复的临时收敛（消灭手工 JSON、自带验证），**不是根因修复**；每次 ctrl 重推/重启后需重跑。根治路径 = QwenPaw 迁移（官方 v1.2.1+ 以插件系统替代了此配置桥接层）。

## 3. 模型路由（为何是 deepseek-direct）

- ctrl 内嵌 AI gateway（:8080）在本部署形态下返回 **Higress 控制台 HTML**（`/health` 200 HTML；`/v1/chat/completions` 404）——不是可用 LLM 端点（上游 embedded 的 gateway AI-provider init 断链，自首次部署即如此）。
- 因此 worker LLM 路径唯一可用形态 = `deepseek-direct`（openai-compat 直连 `https://api.deepseek.com/v1`，模型 `deepseek-chat`）。
- recover-workers.sh 步骤 5c 从 worker 容器内实调 deepseek API 做 200 验证——**配置检查失败即整体失败**，绝不静默回退 gateway。

## 4. 凭据引用（零密钥入库）

| env | 来源（现有秘密源） | 用途 |
|---|---|---|
| `ATB_ADMIN_PASSWORD` | ctrl 容器 env `AGENTTEAMS_ADMIN_PASSWORD`（部署时注入） | ctrl admin（Matrix 登录/管理） |
| `ATB_LLM_KEY` | 生产 Console env `MU_LLM_API_KEY`（部署时注入） | deepseek API（模型直连） |
| worker MinIO 凭据 | ctrl 生成（auth 卷 `agentteams-beta-worker-*-auth`） | worker ↔ MinIO |
| Matrix worker 凭据 | ctrl 生成（同上 auth 卷） | worker Matrix 登录 |

## 5. Console 侧执行器配置（与 worker 栈分开回滚）

Console（`beta-mp-console`）执行器 env：`MU_EXECUTOR=agentteams` + `MU_AGENTTEAMS_BASE_URL`（经 at-auth-proxy）+ `MU_AGENTTEAMS_TOKEN` + `MU_AGENTTEAMS_MATRIX_URL/USER/PASSWORD`（Console 自有 Matrix 账号）+ `MU_AGENTTEAMS_RUNTIME=copaw` + `MU_AGENTTEAMS_MODEL=deepseek-chat`。

- **Console 回滚** = 镜像 tag 级（rc.23 ↔ rc.24），与 worker 栈无关。
- **worker 栈回滚** = `recover-workers.sh` 用上游原 worker tag 重跑（或 shim tag 重跑恢复 shim）——独立于 Console 版本。
- 两组件不得视为同一镜像版本单元。

## 6. 已知债务（如实登记，不宣称已解决）

1. **gateway 404**：未修复（上游 embedded 问题）——deepseek-direct 是**绕过**而非修复；若未来 gateway 可用，可经本清单修订切回（需隔离验证）。
2. **get_soul**：shim 是**运行时补丁**（镜像层，可回滚），非上游修复；上游 copaw-worker 已停更，根治路径是 QwenPaw 迁移（独立评估，不预设结论）。
3. **reconcile 覆盖**：上游行为未改——本真源以顺序契约+工具封装消除"再次丢失配置"的条件（人工不再有机会在 reconcile 前写配置）。
4. **frpc 非持久**（宿主侧）：保活脚本存在、计划任务未注册——独立运维修复，见 RUNBOOK。
