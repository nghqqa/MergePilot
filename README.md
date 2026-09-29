# MergePilot — 自托管 PR 安全审查工作台

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Edition](https://img.shields.io/badge/Edition-Developer_Beta-orange)

MergePilot 是一台跑在你自己机器上的 PR 安全审查工作台：通过 GitHub App **只读**接入你的仓库，
自动同步 Pull Request，运行安全审查，管理发现，验证修复——全程带完整审计追踪。
支持**多用户 RBAC**（五角色）、**DB 持久安全会话**和**可选本地 RAG 增强**。

> **真实 GitHub App 只读链路已在测试 App 上端到端验证**（安装→绑定→真实 PR webhook→PR 快照→卸载级联撤销）。

## 适合谁

- **个人开发者**：想在自己的仓库上跑一套带审计的只读 PR 审查台，2C2G 小机器即可。
- **小团队**：想在内部机器上自托管，多用户分角色协作，把团队安全规范做成可检索的本地语料库（4C8G 起）。
- **不适合**：需要多租户隔离、企业密钥管理或外部合规背书的企业生产环境——这些在 Enterprise Roadmap 中，当前版本未实现。

## 核心能力

| 能力 | 状态 |
|---|---|
| GitHub App 只读接入（安装/绑定/webhook/PR 同步） | ✅ 真实端到端已验证 |
| 多用户 RBAC（Contributor / Reviewer / Maintainer / PlatformAdmin / Auditor） | ✅ |
| DB 持久安全会话（重启可恢复、撤销即时生效、HttpOnly/Lax/Prod Secure） | ✅ |
| 邀请制 onboarding（GitHub 数字 user id 绑定，无公共自动注册） | ✅ |
| Review Agent（只读 PR 审查 + 阶段推导） | ✅ |
| AgentTeams 执行器（外部 runtime 四 Agent：leader/reviewer/fixer/verifier；AgentTeams-first fail-closed） | ⚙️ 受控 Beta（2h 稳定性验证，长期 soak 未做） |
| Console（React SPA + 实时控制台） | ✅ |
| FXV 修复验证（dry-run 默认 + 真 git 隔离环境） | ⚙️ 受控 |
| 本地 RAG 语料检索（reference-only，`local-hash-v1` 默认） | ⚙️ 试用 |
| Docker 部署（离线镜像包 / 源码自建） | ✅ |

## 能力边界（诚实声明）

- ❌ **不自动 merge** — 所有合并决策由人工执行
- ❌ **不自动 approve** — 审批由人工操作
- ❌ **GitHub App 权限仅 read-only** — 不申请任何 write/administration/actions
- ❌ **RAG 只返回 reference** — 检索结果不自动生成 finding/ticket/gate/VERIFIED
- ❌ **默认不做任何模型下载** — `local-hash-v1` 零下载；语义模型须自带并过 manifest 校验
- ❌ **无 RLS 行级隔离** — 应用层租户约束（非 PostgreSQL RLS）
- ❌ **无 SSO/SCIM/HA/配额限流** — Developer Edition 不含
- ℹ️ **AgentTeams 四 Agent 仅产出建议** — Reviewer/Fixer/Verifier 输出为 LLM 建议（脱敏 finding 摘要输入）；Fixer 仅 dry-run 文本（不应用不提交）；Verifier 判定为模型意见（无代码执行/测试证据时不构成"修复已验证"）；最终裁决由 MergePilot 内部 Leader 做出
- ℹ️ **AgentTeams 为正式执行路径** — `MU_EXECUTOR=agentteams`；未配置/不健康/worker 不完整即 fail-closed 拒绝（不回退 internal）；internal 仅限开发/测试/显式 emergency（须 `MU_EXECUTOR_INTERNAL_ALLOW` 开关）
- ℹ️ **AgentTeams 栈 Controller 重启后需人工重跑 provision** — 见 deploy/agentteams-beta/RUNBOOK.md §3/§13

## Quickstart

### 路径 A：预构建发行包（2C2G）

```bash
cd distribution/docker
docker load -i mp-console-image.tar   # 导入离线镜像（约 60MB）
cp .env.example .env
# 编辑 .env 填入生成的密钥（参见 BETA-GUIDE）
docker compose up -d
# 访问 http://127.0.0.1:4730
```

### 路径 B：源码自建 + GitHub App + 本地 RAG（4C8G）

```bash
cd deploy/local-rag-trial
cp .env.example .env
# 编辑 .env：填入 GitHub App 凭据 + RAG scope（参见 BETA-GUIDE）
docker compose up -d --build
# 访问 http://127.0.0.1:48450
```

→ **完整安装指南（GitHub App 创建/权限/事件/凭据注入/首次登录/邀请/绑定）**：[BETA-GUIDE.md](docs/BETA-GUIDE.md)

## 架构

```
GitHub ──(GitHub App read-only)──► Webhook (HMAC 验签) ──► Console (Node.js)
                                                                   │
                    ┌──────────────────────────────────────────────┤
                    │                    │                         │
                  PG (13+ 表)         MinIO (证据包)         React SPA
               (mu schema 六版迁移)   (内容寻址)
                    │
                    ▼ （MU_EXECUTOR=agentteams，fail-closed）
        AgentTeams Runtime（外部，Beta 受控）
        ├─ mergepilot-leader（编排建议）
        ├─ mergepilot-reviewer（审查建议）
        ├─ mergepilot-fixer（dry-run 修复建议）
        └─ mergepilot-verifier（独立验证意见）
                    │ 全部输出仅建议 —— MergePilot Leader 终裁
                    ▼
        Controller API + Matrix 任务传输（见 deploy/agentteams-beta/RUNBOOK.md）
```

详见 [ARCHITECTURE.md](distribution/docs/ARCHITECTURE.md)

## Developer Edition Beta 与 Enterprise 的边界

| 维度 | Developer Edition Beta（当前） | Enterprise（路线图，未实现） |
|---|---|---|
| 租户模型 | 应用层 tenant_id 约束 + DB 复合 FK | PostgreSQL RLS 行级隔离 |
| 身份 | GitHub OAuth + 邀请制 + 本地 fixture（测试） | SSO (SAML/OIDC) + SCIM |
| 仓库接入 | GitHub App read-only | 同左 + 写权限（独立设计） |
| 合并 | ❌ 不自动 merge/approve | 受控合并执行面（独立设计） |
| Secret 管理 | env / .env 文件 | Secret Manager 集成 |
| 部署 | Docker Compose 单机 | HA + K8s |
| 配额限流 | ❌ | ✅ |

## 本地 RAG 增强

- 语料导入/索引/检索/删除/回滚：[LOCAL-RAG-GUIDE](distribution/docs/LOCAL-RAG-GUIDE.md)
- 嵌入策略（`local-hash-v1` 默认 / `bge-m3` 可选 8C16G）：[LOCAL-RAG-GUIDE](distribution/docs/LOCAL-RAG-GUIDE.md#嵌入策略)
- 状态与红线：[RAG-STATUS](distribution/docs/RAG-STATUS.md)

## 运维

| 主题 | 文档 |
|---|---|
| API 契约 | [API-CONTRACTS](distribution/docs/API-CONTRACTS.md) |
| 安全 | [SECURITY](distribution/docs/SECURITY.md) |
| 审计 | [AUDIT](distribution/docs/AUDIT.md) |
| 备份/回滚/卸载 | [ROLLBACK](distribution/docs/ROLLBACK.md) |
| 监控 | [MONITORING](distribution/docs/MONITORING.md) |
| 已知限制 | [LIMITATIONS](distribution/docs/LIMITATIONS.md) |
| 完整安装+配置指南 | [BETA-GUIDE](docs/BETA-GUIDE.md) |

## 安全声明

- 所有凭据（private key / webhook secret / OAuth secret）仅通过环境变量注入，绝不入库、日志或审计
- 数据库只存储 sha256 摘要（session token / CSRF / OAuth state / correlation cookie）
- Cookie 安全：HttpOnly + SameSite=Lax + Path=/，生产模式强制 Secure
- CSRF 防护：双提交（可读 cookie + 请求头 timing-safe 比较）
- 审计事件白名单制：tenant 域与 platform 域分立，未知 kind 一律拒绝

## 反馈

- Bug / Feature Request：[GitHub Issues](https://github.com/nghqqa/MergePilot/issues)
- 安全漏洞：请勿公开 Issue，参考 [SECURITY.md](distribution/docs/SECURITY.md) 中的报告流程

## License

Apache 2.0 — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## 旧版本说明

> 此仓库此前包含比赛/演示版本的代码（tag: `legacy/pre-v0.1.0`）。
> v0.1.0+ 是产品化版本，架构和能力边界与旧版有显著差异。
