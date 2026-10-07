# MergePilot — 自托管 PR 安全审查工作台

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/badge/Release-v0.2.0--beta.6--rc.17-0e6b62)](https://github.com/nghqqa/MergePilot/releases/tag/v0.2.0-beta.6-rc.17)
![Edition](https://img.shields.io/badge/Edition-Developer_Beta-orange)

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./docs/assets/readme/hero-dark.svg">
    <img src="./docs/assets/readme/hero-light.svg" width="100%" alt="MergePilot 审查管线：PR 一提交审查自动开始；多 Agent 审查输出建议；高危变更在人工闸门停下等审批；合并权永远在人手里">
  </picture>
</p>

MergePilot 跑在你自己的服务器上：通过 GitHub App **只读**接入仓库，PR 事件自动触发审查管线；
发现与证据全程留痕，高危变更停下等人工审批。它没有 GitHub 写权限，不能代替你合并或批准——
所有 Agent 输出仅为**建议**，是否合并永远由维护者在 GitHub 上决定。

## 界面

![MergePilot Console 总览：统计卡与 PR 列表](./docs/assets/readme/console-overview-rc16.png)

<details>
<summary>图注与页面说明</summary>

图为维护者**测试环境的真实历史数据**（公开测试仓库 `test-repo`；「已阻断 / 异常」是演练产生的
真实状态，不是修饰过的演示数据）；右上角账号 / 组织 / 角色徽章已打码。统计卡即过滤入口
（数字 = 列表行数，按 PR 去重）；每个 PR 一行展示当前/latest head 的风险与阶段，历史 head 与
审查 run 在「查看详情」抽屉展开，数据每 30s 刷新。

</details>

## 解决什么问题

- **AI 审查的结论不该直接可信** —— Reviewer / Fixer / Verifier 的输出都按"建议"处理，附可核查的证据；管线状态由 MergePilot 内部 Leader 终裁（通过 / 返工 / 阻断），而**合并决定永远由维护者做出**——两者是不同层级的裁决。
- **高危变更不能静默通过** —— 修复验证默认 dry-run；高危 finding 的修复需维护者审批票（仅维护者可放行）。
- **代码与凭据不应失控出站** —— 单机自托管、GitHub App 只读、数据落在你自己的 PostgreSQL。自托管**不等于零出站**：审查管线需要外部 LLM 时会有受控外发，见[数据出站边界](#数据出站边界)。

## 适合谁

- **个人开发者**：给自己的仓库加一台带审计的只读审查台（console + 数据库基础档 2C2G）。
- **小团队**：内网自托管、五角色 RBAC 协作；可选把团队规范做成可检索的本地语料（含 RAG sidecar 推荐 4C8G 起）。
- **暂不适合**：需要 RLS 行级隔离、SSO/SCIM、HA 或配额限流的企业生产环境——见[能力边界](#能力边界请务必阅读)与下文对照表。

## 上手

前置条件：

- Docker Engine 24+ / Docker Compose v2；Node.js ≥ 20（跑 preflight 检查）
- **GitHub App + OAuth App 各一个**（前者收 webhook、后者做用户登录，两个都要建）
- **外部 LLM 端点**（OpenAI 兼容）：审查管线必填（默认 `disabled` 零网络；显式配置后才启用）
- **公网可达的 HTTPS 入口**：想自动接收 GitHub webhook 就必须有（Nginx / Caddy / Cloudflare Tunnel 等任意反代或隧道，实现自选），转发到本机 `48590`；没有公网入口时本地管理界面可用，但 webhook 进不来
- 可选：AgentTeams 执行器（修复验证环节用）、bge-m3 RAG、cchain 三键
- 默认禁用项（保持即安全，启用需显式配置）：fixture 登录（`MU_ALLOW_FIXTURE_LOGIN=1`）、修复真实写入、`MERGEPILOT_CCHAIN_ENFORCE` 拦截

```bash
git clone https://github.com/nghqqa/MergePilot.git
cd MergePilot/deploy/selfhost
cp .env.example .env                    # 按注释填写必填段，chmod 600 .env
node preflight.mjs                      # 只读体检：关键项失败即退出并说明缺什么
docker compose --env-file .env up -d    # 首次启动自动建表（mu schema v23）
node preflight.mjs --live               # 活体探测：health / schema / attest / queue
```

身份初始化（依据 rc.16 源码 `mu/store.mjs bootstrap()` 与 OAuth 回调，`v0.2.0-beta.6-rc.16` 与
main 后端零差异）：启动即自动创建 `default` 租户和一个 **fixture 引导操作员**（默认登录名
`pilot-admin`，仅 `MU_ALLOW_FIXTURE_LOGIN=1` 时可登录——生产保持关闭）；GitHub OAuth 登录
**只接受事先创建的邀请**（无邀请即 `not_invited`，无公共自动注册）；邀请永不授予
`platform_admin`（API / claim / DB 三层拒绝）——**首个平台管理员需部署者在数据库直授**，
属设计行为。分步操作见 [BETA-GUIDE §6](docs/BETA-GUIDE.md)。

**"容器起来了"不等于"审查链路就绪"**。`preflight.mjs --live` 覆盖 health / schema / 会话门 /
非终态积压 / attest 端点；下表其余验收项 **preflight 不覆盖**，需自行确认：

| 就绪项 | 验收判据 | preflight 覆盖 |
|---|---|---|
| Console 健康 | `--live` 全绿；`/api/health` version 为 `0.2.0-beta.6-rc.16` | ✅ |
| 身份初始化 | default 租户已建；首个平台管理员数据库直授完成；受邀用户可登录认领 | ❌ |
| Webhook 接收 | GitHub App 高级页 Recent Deliveries 显示 2xx（可用 Redeliver 做端到端验证；无公网入口时永远为空） | ❌ |
| 审查 LLM | `MU_LLM_*` 指向的端点真实可调用（preflight 只查配置形态，不发真实请求） | ❌ |
| 修复验证执行器（可选） | `MU_EXECUTOR=agentteams` 且 controller / Matrix 健康检查通过；未配置时该环节 fail-closed | ❌ |

不想 git clone，也可以用发行包起步——推荐 **r2 修订包**：

| 附件 | 说明 |
|---|---|
| `mergepilot-selfhost-deploy-kit-rc16.1-r2.tar.gz` | **推荐**。自托管套件（compose / .env.example / preflight / attest 引导），compose 已钉定 rc.16 官方镜像 digest，与 console 离线 tar 同源 |
| `SHA256SUMS-rc16.1-r2.txt` | r2 kit 校验和 |
| `mergepilot-console-rc16.tar` | 离线镜像（`docker load`，与 GHCR 镜像同源） |
| `rc16-sbom.cdx.json` / `scan-rc16.json` | CycloneDX SBOM（35 组件）/ trivy 扫描存档（2026-10-06 时点：漏洞 0 / 秘密 0，不构成永久无漏洞声明） |
| `mergepilot-selfhost-deploy-kit-rc16.tar.gz` + `SHA256SUMS-rc16.1.txt` | 首发旧 kit，**仅作历史存档**（compose 为 rc.15 基线），不要用作安装入口 |

**版本口径，别互相替代**：

- 运行时源码 tag：`v0.2.0-beta.6-rc.16`（与镜像同源）；镜像内置版本 `0.2.0-beta.6-rc.16`（`/api/health` 的 `version` 字段为真源）
- 发行 tag：`v0.2.0-beta.6-rc.16.1`（仅追加文档与发布资产，运行时与 rc.16 零差异）
- 部署 kit：以上表 r2 修订包为准（首发旧 kit 存档）；当前 `main` 分支含最新文档收口，比发行 tag 新
- 镜像 digest、tar SHA256、SBOM/扫描档案各自独立——校验方法见 [deploy/selfhost/README.md「镜像获取与校验」](deploy/selfhost/README.md)

## 架构

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./docs/assets/readme/architecture-dark.svg">
    <img src="./docs/assets/readme/architecture-light.svg" width="100%" alt="部署拓扑：GitHub App 只读接入，webhook ingress 验签去重后交给 Console；数据在仅内网的 PostgreSQL；可选 bge-m3 RAG sidecar 与独立部署的 AgentTeams 执行器；cchain 三层校验任一缺失即 BLOCKED">
  </picture>
</p>

- **接入**：GitHub App 5 项权限全只读（Metadata / Contents / Pull requests / Checks / Statuses），订阅 5 种事件；系统无 merge / approve 权限，结论只在 Console 呈现。
- **入口**：webhook ingress 仅放行 webhook 与 health 两个路径，HMAC 验签 + delivery 一次性去重。
- **数据**：PostgreSQL + pgvector，八张业务表按 `tenant_id` 强制收窄（应用层隔离）；数据库只存会话 / CSRF / OAuth state 的 sha256 摘要。
- **审查管线**：进程内 Reviewer（LLM 建议）+ 确定性规则发现；Fixer 为 dry-run 真子进程、Verifier 为确定性验证——这两环不经 LLM。管线状态由内部 Leader 终裁，是否合并由维护者决定。
- **修复验证执行器**：正式路径为**独立部署**的 AgentTeams runtime（[RUNBOOK](deploy/agentteams-beta/RUNBOOK.md)）；未配置、不健康或 worker 不完整时该环节 fail-closed 拒绝，不回退。
- **cchain（可选）**：模型缓存完整性 / 供应商证明 / keystore 运行绑定三层。任一缺失 → `BLOCKED`（状态面如实呈现阻塞条件，默认**不影响审查主链**）；若显式设置 `MERGEPILOT_CCHAIN_ENFORCE=1`，BLOCKED 会拒绝启动修复验证 run。attestation 是部署者对本次部署的自证，不是第三方签名。配置引导见 [deploy/selfhost/README.md](deploy/selfhost/README.md)。

### 数据出站边界

自托管 ≠ 所有数据零出站。默认 `MU_LLM_PROVIDER=disabled` 时零网络请求；显式配置
`openai_compatible` 端点后，审查管线会把以下内容发给**你配置的 LLM 服务**（代码级白名单，
全部标记为 untrusted_data）：

- PR 元数据：编号、标题（截断脱敏）、变更文件数、head SHA 前 12 位、base 分支名；
- 确定性 finding 摘要（≤50 条）：规则 ID、严重级、文件路径、行号、脱敏标题/摘要；
- 与 finding 相关的 diff 代码片段（总载荷 ≤ 24KB，超出先丢弃 hunks 再截 findings；常见密钥格式已掩码）。

不含仓库名/组织名、其他文件、凭据与环境变量；错误与日志只含 reason code + digest，不落 prompt 与响应正文。
AgentTeams 执行器路径下，外发给 runtime 的审查任务同为脱敏 finding 摘要。

## 关键能力

- **接入与审查**
  - GitHub App 只读接入：安装 / 绑定 / webhook / PR 同步
  - 审查管线：确定性规则发现 + Reviewer（LLM 建议，输出过严格 schema 校验），内部 Leader 终裁
  - FXV 修复验证：默认 dry-run + 真 git 隔离环境；高危 finding 修复需审批票；真实写入默认关闭
- **多用户与租户**
  - 五角色 RBAC（Contributor / Reviewer / Maintainer / PlatformAdmin / Auditor）+ DB 持久会话（重启可恢复、撤销即时生效）
  - 邀请制 onboarding（GitHub 数字 user id 绑定，无公共自动注册；`platform_admin` 不可经邀请授予）
  - 应用层多租户隔离：八表按 tenant 收窄、installation 与租户绑定、撤权后同会话下一请求即 403
- **本地 RAG（可选）**
  - 语料导入 / 索引 / 检索 / 回滚；默认 `local-hash-v1`（确定性哈希，零模型下载）
  - `bge-m3` 语义嵌入可选（模型工件自带约 2.3GB + manifest 校验）；检索结果 reference-only
- **供应链**
  - digest 钉死官方镜像 + 离线 tar；CycloneDX SBOM；trivy 扫描存档；SHA256SUMS 逐附件校验

资源构成：基础档 = console + PostgreSQL（2C2G）；+ bge-m3 sidecar 推荐 4C8G，语义嵌入 8C16G；
AgentTeams runtime 为独立部署组件，资源需求见其 RUNBOOK。未承诺安装耗时。

## 能力边界（请务必阅读）

- ❌ **不自动 merge / 不自动 approve** —— 合并与审批由人在 GitHub 上执行；内部 Leader 的终裁只作用于管线状态，不触及你的仓库
- ❌ **GitHub App 仅只读权限** —— 不申请任何 write / administration / actions，不回写 check run
- ❌ **Agent 输出仅为建议** —— Reviewer 输出为 LLM 建议（输入为脱敏上下文，见[数据出站边界](#数据出站边界)）；Fixer 为 dry-run 文本（不应用不提交）；Verifier 判定是模型/规则意见，无代码执行 / 测试证据时不构成"修复已验证"
- ❌ **RAG 只返回 reference** —— 检索结果不自动生成 finding / ticket / gate / VERIFIED；默认零模型下载
- ❌ **无 PostgreSQL RLS / SSO / SCIM / HA / 配额限流** —— 多租户为应用层约束（复合 FK + 查询收窄），Developer Edition 不含企业能力
- ℹ️ **AgentTeams 是修复验证的正式执行路径**（`MU_EXECUTOR=agentteams`，fail-closed）；Controller 重启后需人工重跑 provision，见 [RUNBOOK §3/§13](deploy/agentteams-beta/RUNBOOK.md)

## Developer Edition 与企业能力的边界

| 维度 | Developer Edition Beta（当前） | Enterprise（路线图，未实现） |
|---|---|---|
| 租户模型 | 应用层 `tenant_id` 约束 + DB 复合 FK | PostgreSQL RLS 行级隔离 |
| 身份 | GitHub OAuth + 邀请制 | SSO (SAML/OIDC) + SCIM |
| 仓库接入 | GitHub App read-only | 同左 + 写权限（独立设计） |
| 合并 | ❌ 不自动 merge / approve | 受控合并执行面（独立设计） |
| Secret 管理 | env / .env 文件 | Secret Manager 集成 |
| 部署 | Docker Compose 单机 | HA + K8s |
| 配额限流 | ❌ | ✅ |

## 文档

| 主题 | 文档 |
|---|---|
| 完整安装与配置（GitHub App / 凭据注入 / 邀请 / 绑定 / RAG / FXV / 备份） | [docs/BETA-GUIDE.md](docs/BETA-GUIDE.md) |
| 自托管部署（镜像校验 / 配置参考 / attest 四步引导 / 公网入口 / 升级回滚 / 备份） | [deploy/selfhost/README.md](deploy/selfhost/README.md) |
| 本地 RAG 指南（嵌入策略 / 状态与红线） | [LOCAL-RAG-GUIDE](distribution/docs/LOCAL-RAG-GUIDE.md) · [RAG-STATUS](distribution/docs/RAG-STATUS.md) |
| AgentTeams 运行手册 | [deploy/agentteams-beta/RUNBOOK.md](deploy/agentteams-beta/RUNBOOK.md) |
| 架构 / API / 安全 / 审计 / 监控 / 已知限制 | [distribution/docs/](distribution/docs/)（ARCHITECTURE · API-CONTRACTS · SECURITY · AUDIT · MONITORING · LIMITATIONS） |
| 更新日志 | [CHANGELOG.md](CHANGELOG.md) |

## 反馈与贡献

- Bug / Feature Request：[GitHub Issues](https://github.com/nghqqa/MergePilot/issues)
- 安全漏洞：**请勿在公开 Issue 披露细节**——请通过 Issue 仅留联系方式（不含漏洞信息）由维护者接洽。仓库未确认启用 GitHub 私密漏洞报告；一旦启用会在本节更新。安全设计说明见 [SECURITY](distribution/docs/SECURITY.md)
- 贡献：[CONTRIBUTING.md](CONTRIBUTING.md)

## License

Apache-2.0 — see [LICENSE](LICENSE)

## 旧版本说明

> 此仓库此前包含比赛/演示版本的代码（tag: `legacy/pre-v0.1.0`）。
> v0.1.0+ 是产品化版本，架构和能力边界与旧版有显著差异。
