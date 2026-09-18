# Release 描述 · 离线 Docker 镜像交付（粘贴到 Release 正文）

> 使用前必读：四种获取方式的定位不同，按需选择；数据能力边界见文末，不可协商。

---

## 四种获取方式（按投入递增）

### ① Demo 包 — 无需 Docker（最快验证）
`MergePilot-demo.zip` / 仓库 `demo-platform/`：Node 直启的回放控制台，
54/54 自测可复现，三条安全路径 + 云端 Trace 记录回放。
**不需要 Docker、不需要网络、不触碰真实 GitHub。**
→ 适合：快速了解产品形态、评审演示。

### ② Offline image bundle — 完整隔离栈（本包核心）
`MergePilot-images-linux-amd64.tar.zst` + `MergePilot-images-manifest.json` + `SHA256SUMS`：
9 个镜像（8 个本地构建 + 1 个 digest 钉死的 pgvector），覆盖 Controller /
Policy Gateway / PostgreSQL(pgvector) / gh-webhook / demo-console / console-edge /
preflight，以及可选真实 GitHub 链路的 gh-proxy / mcp-bridge。
加载：`load-images.sh`（WSL2/Linux）或 `load-images.ps1`（Windows 11 + Docker Desktop），
逐镜像校验数量 / tag / digest / 架构后启动 `docker compose up`。
→ 适合：在自己的环境运行完整隔离栈与真实接入链路。

### ③ GHCR — 联网可选（预留）
`ghcr.io/nghqqa/mergepilot-isolated-*`：联网环境可改用 `docker pull`（digest 以
镜像清单为准）。**当前版本未发布到 GHCR，请使用 ②**；发布后本节更新为逐镜像 pull 命令。

### ④ GitHub integration — 需要你自行配置
GitHub App / Webhook / GitHub MCP 真实接入：PAT 只注入 github-mcp（网络隔离），
coordinator 只读，fixer 只写 `fix/*` 分支；一切 secret 走编排器生成的 env 文件。
逐步操作：[docs/real-loop-your-repo.md](https://github.com/nghqqa/MergePilot/blob/main/docs/real-loop-your-repo.md)。
→ 适合：把闭环指向你自己的仓库。

---

## 真实能力边界（如实声明，不可协商）

| 能力 | 状态 |
| --- | --- |
| PolarDB 接入 | **NOT CONNECTED**——8 项接入门槛已在代码中预留，条件齐备切 LIVE 无需改代码 |
| Database Branch（create_branch / validate_migration / assert_data / rollback_check） | **SIMULATED**——隔离模拟环境上的验证状态机；未对接真实数据库 |
| PR Auto Merge | **DISABLED**——系统从不自动合并；fixer 只写 `fix/*` 分支 |
| RAG | **SYNTHETIC / REDACTED**——合成语料，检索与防伪机制真实 |
| 云端 Trace | AgentLoop 云端 Trace 为已确认样本（n=1），非当前实时数据 |

离线镜像包经三层扫描（环境变量 / 构建历史 / 文件系统）：无真实凭据；
运行时凭据（LLM key / GitHub PAT / MinIO / Matrix）一律部署时注入，不在镜像内。
