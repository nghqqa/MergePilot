# MergePilot Developer Edition Beta — 完整安装与配置指南

本文档覆盖从零到完整使用的全部步骤，包括 GitHub App 创建、凭据注入、首次登录、邀请用户、仓库绑定和 PR 审查。

> **能力口径（rc.35.5）**：当前产品为**完整受控闭环**——webhook→PR 快照→静态规则审查（precheck）→
> LLM 第二阶段审查（模型审查，解耦执行）→发现落库→裁定与修复审批门→补丁版本化与验证
> （真实可应用性五态 + 实际修复测试）→维护者逐次「预览 → 确认」完成远端写：
> **发布审查评论 / 应用修复（机器人提交，新 head 自动重审 + CI 资格判定）/ 确认合并
> （统一资格门）**。
> 旧文档中「只读快照模式」「应用与合并开发中」的描述已过时。按目标分步的部署/使用/排障/验收见
> **[docs/delivery/](delivery/README.md)**（能力矩阵、部署指南、用户指南、排障手册、验收清单、演示脚本），
> 配套只读诊断工具 [`scripts/delivery/diagnose.mjs`](../scripts/delivery/diagnose.mjs)。

---

## 目录

1. [产品定位](#1-产品定位)
2. [Developer Edition Beta 与 Enterprise 的边界](#2-developer-edition-beta-与-enterprise-的边界)
3. [Docker Compose 快速启动](#3-docker-compose-快速启动)
4. [GitHub App 创建与配置](#4-github-app-创建与配置)
5. [环境变量配置](#5-环境变量配置)
6. [首次登录与邀请用户](#6-首次登录与邀请用户)
7. [仓库绑定与 PR 审查流程](#7-仓库绑定与-pr-审查流程)
8. [本地 RAG 增强启用](#8-本地-rag-增强启用)
9. [FXV 修复验证](#9-fxv-修复验证)
10. [备份、升级、回滚与卸载](#10-备份升级回滚与卸载)
11. [常见故障排查](#11-常见故障排查)
12. [Beta 已知限制](#12-beta-已知限制)
13. [安全声明](#13-安全声明)
14. [反馈方式](#14-反馈方式)

---

## 1. 产品定位

MergePilot 是**自托管的 PR 安全审查工作台**：

- 接入你的仓库：GitHub 走 App（审查只需一组只读权限；启用应用修复/合并才需 Contents 与
  Pull requests 的读写权限，见 §4.2）；Gitee 走部署级 PAT 连接（Wave-Gitee）
- 自动同步 Pull Request（webhook 驱动，事件自动消费）
- 运行两阶段审查：确定性静态规则（precheck）+ 可选 LLM 第二阶段（模型发现独立标注 `reviewer`）
- 产出结构化发现与**版本化建议补丁**，并给出验证三分（格式检查 / 真实可应用性 / 实际修复测试）
- 修复需维护者逐条审批（P0/P1 审批门），修复轮 dry-run 零远端写入
- 维护者逐次「预览 → 确认」完成三类受控远端写：**发布审查评论 / 应用修复（机器人以自己的
  身份提交修复 commit → 新 head 自动重审）/ 确认合并（资格门：当前 head 审查完成 + 真实 CI
  通过 + mergeable 已知）**（机器人身份，幂等与防重复写设计）
- 全程带完整审计追踪（tenant 域 + platform 域分立）
- 可选开启本地 RAG 增强（语料库检索为审查提供可引用的参考证据）

**核心理念**：数据与部署在**你自己的机器**；GitHub App 权限最小化；补丁永远是建议；
不自动改代码、不自动 merge、不自动 approve。**远端写共三类，全部经维护者逐次确认**
（机器人身份执行）。启用 LLM 第二阶段会把 PR 上下文出站到你配置的模型端点——
需租户级 `external_api` 出站策略与显式同意记录（`evidence_only` 模式零出站）。

## 2. Developer Edition Beta 与 Enterprise 的边界

| 能力 | Developer Beta（当前） | Enterprise（未实现） |
|---|---|---|
| 多用户 RBAC | ✅ 五角色应用层（decide_review 仅 maintainer） | RLS 行级隔离 |
| 身份 | GitHub/Gitee OAuth + 邀请制 | SSO (SAML/OIDC) + SCIM |
| 仓库接入 | GitHub App（审查只读权限；应用/合并另需 Contents/PR 读写）+ Gitee 连接 | 独立写权限设计 |
| 审查深度 | 静态规则 + LLM 第二阶段（需显式出站授权） | — |
| 远端写 | 三类受控写：评论 / 机器人提交修复 / 合并 PR（均逐次人工确认） | 策略化/批量授权流 |
| 会话 | DB 持久（单机） | HA + 集中密钥管理 |
| 配额限流 | ❌ | ✅ |
| 自动 approve/merge | ❌ 不存在自动批准/自动合并路径（合并=人工逐次确认） | 预约/策略化合并 |

**当前版本不是 Enterprise Production Ready。**

## 3. Docker Compose 快速启动

> **canonical 部署入口**：[`deploy/selfhost/`](../deploy/selfhost/README.md)（console + postgres +
> bge-m3 sidecar + webhook ingress 四服务拓扑，镜像 digest 校验与 preflight 一应俱全）。
> 分步操作（最小部署→按需启用）见 [delivery/DEPLOY-GUIDE.md](delivery/DEPLOY-GUIDE.md)。
> 以下两条为早期路径，保留可用。

### 前提

- Docker Engine 24+ / Docker Compose v2
- 2GB 可用内存（基础）或 4GB（含 RAG）

### 路径 A：预构建发行包

```bash
cd distribution/docker
docker load -i mp-console-image.tar
cp .env.example .env
# 编辑 .env（参见 §5 环境变量配置）
docker compose up -d
curl http://127.0.0.1:4730/api/health   # 验证 200
```

### 路径 B：源码自建（含 GitHub App + RAG）

```bash
cd deploy/local-rag-trial
cp .env.example .env
# 编辑 .env（填入 GitHub App 凭据，参见 §4-§5）
docker compose up -d --build
curl http://127.0.0.1:48450/api/health  # 验证 200
```

> **安全设计**：console 端口默认仅绑定 `127.0.0.1`（回环）。外部访问须显式修改 ports 映射前缀并配套防火墙+TLS。

## 4. GitHub App 创建与配置

### 4.1 创建 GitHub App

1. 打开 https://github.com/settings/apps/new
2. 填写：

| 字段 | 值 |
|---|---|
| GitHub App name | 你的 App 名称（如 `my-mergepilot`） |
| Homepage URL | 你的 console 地址（如 `http://127.0.0.1:48450`） |
| Callback URL | `http://<你的地址>/api/mu/github/install/callback` |
| Webhook URL | `http://<你的地址>/api/mu/github/webhook`——对外部署时该地址**必须公网可达**（HTTPS 入口转发到本机 48590；Nginx/Caddy/Cloudflare Tunnel 等任意反向代理或隧道均可） |
| 受邀成员登录 | `https://<console 公开地址>/login`（或带邀请参数 `/login?invite=<invite_id>`，管理员在控制台「成员与邀请」面板创建邀请后一键复制；须为 **https 公网** 地址，与 OAuth Callback 同域） |
| Webhook secret | 自定义随机字符串（如 `openssl rand -hex 32`） |

### 4.2 权限（按能力分两档，只开你需要的）

**只读档（审查主链必需）**：

| 权限 | 级别 | 用途 |
|---|---|---|
| Metadata | **Read-only** | 安装仓库列表 |
| Contents | **Read-only** | 读 diff 与文件内容 |
| Pull requests | **Read-only** | 读 PR 上下文 |
| Checks | **Read-only** | 读 check runs（合并资格的 CI 判定面之一） |
| Commit statuses | **Read-only** | 读 commit status（CI 判定另一面） |
| Administration | **Read-only** | 分支保护只读探测 |

**写侧档（仅当你要用「应用修复 / 确认合并」时勾选；缺任一项对应能力在界面报告
unsupported，不影响审查）**：

| 权限 | 级别 | 用途 |
|---|---|---|
| Contents | **Read and write** | 机器人向 PR 分支推送修复 commit |
| Pull requests | **Read and write** | 合并 PR |

> 在 App 设置页更新权限后，仓库安装会提示重新授权（Revoke & request），接受后生效。
> ⚠️ **仍然不勾 Administration:write / Members / Actions 等任何其他权限**；
> 系统不会用个人 PAT 绕过缺失的 App 权限——缺什么在界面如实报告什么。

### 4.3 事件订阅（仅勾这 5 个）

- ✅ Installation
- ✅ Installation repositories
- ✅ Pull request
- ✅ Check run
- ✅ Status

### 4.4 安装限制

选择 **Only on this account**（推荐测试用途）或 Any account。

### 4.5 创建后获取凭据

| 凭据 | 来源 | 对应环境变量 |
|---|---|---|
| App ID | 创建成功页 | `MU_GITHUB_APP_ID` |
| App Slug | URL 中 `github.com/settings/apps/{slug}` 的 `{slug}` | `MU_GITHUB_APP_SLUG` |
| Private Key | 页面 → Private keys → Generate → 下载 `.pem` | `MU_GITHUB_APP_PRIVATE_KEY` |
| Webhook Secret | 你在 4.1 中设置的 | `MU_GITHUB_WEBHOOK_SECRET` |
| Install Callback URL | 与 4.1 中的 Callback URL 相同 | `MU_GITHUB_APP_INSTALL_CALLBACK_URL` |

## 5. 环境变量配置

编辑 `.env` 文件（从 `.env.example` 复制）。**所有真实凭据绝不提交仓库。**

### 基础配置

```bash
MU_MODE=multiuser
CONSOLE_PG_DSN=postgres://postgres:<密码>@pg:5432/ragtrial
CONSOLE_SESSION_SECRET=<openssl rand -hex 32>
```

### GitHub App（必填用于 App 接入）

```bash
MU_GITHUB_APP_ID=<你的 App ID>
MU_GITHUB_APP_SLUG=<你的 App slug>
MU_GITHUB_APP_PRIVATE_KEY=<.pem 文件全部内容，含 BEGIN/END 行>
MU_GITHUB_WEBHOOK_SECRET=<你在 GitHub App 设置的 webhook secret>
MU_GITHUB_APP_INSTALL_CALLBACK_URL=http://<你的地址>/api/mu/github/install/callback
```

### OAuth 登录（可选——不用 GitHub 登录可跳过）

```bash
MU_GITHUB_OAUTH_CLIENT_ID=<OAuth App client_id>
MU_GITHUB_OAUTH_CLIENT_SECRET=<OAuth App client_secret>
MU_GITHUB_OAUTH_CALLBACK_URL=http://<你的地址>/api/mu/auth/oauth/github/callback
```

### Gitee OAuth 登录（可选——Wave-Gitee）

与 GitHub OAuth 同构的 authorization-code 流程（`gitee-oauth:<数字id>` 身份前缀，与
GitHub 身份空间隔离不互认）。三项任一缺失 → `providers.gitee.configured:false`，
登录页不渲染 Gitee 入口（fail-closed）：

```bash
MU_GITEE_OAUTH_CLIENT_ID=<Gitee OAuth 应用 Client ID>
MU_GITEE_OAUTH_CLIENT_SECRET=<Gitee OAuth 应用 Client Secret>
MU_GITEE_OAUTH_CALLBACK_URL=http://<你的地址>/api/mu/auth/oauth/gitee/callback
```

Gitee 应用创建：gitee.com → 设置 → 第三方应用 → 创建应用；授权回调地址填上面的
`MU_GITEE_OAUTH_CALLBACK_URL`，scope 使用 `user_info`（仅读数字 id 与 login）。
受邀成员若绑定 Gitee 身份，邀请的 `expected_subject` 须为 `gitee-oauth:<数字id>`
（邀请管理面板提供 GitHub/Gitee 平台选择）；受邀者从登录页 Gitee 入口进入。
已有账号绑定 Gitee 身份：设置页 → 账号绑定 → 绑定 Gitee 账号（完整 Gitee 授权；
仅增一条登录路径，租户/成员关系/审批能力不变）。

### 会话与安全（可选覆盖）

```bash
MU_SESSION_TTL_MS=28800000      # 会话 TTL（默认 8h）
MU_OAUTH_FLOW_TTL_MS=600000     # OAuth state 有效期（默认 10min）
```

### 执行器（AgentTeams-first，2026-09-29 起）

正式/Beta 路径必须使用外部 AgentTeams Runtime；未配置、健康检查失败、认证失败或 worker 不完整时 **fail-closed 拒绝执行（不静默回退 internal）**：

```bash
MU_EXECUTOR=agentteams                        # 正式路径（唯一生产选项）
MU_AGENTTEAMS_BASE_URL=http://<agentteams-controller>:8090
MU_AGENTTEAMS_TOKEN=<部署级 Bearer token>
MU_AGENTTEAMS_RUNTIME=copaw                   # 与目标集群 runtime 对齐（如 copaw）
MU_AGENTTEAMS_MODEL=deepseek-chat             # 与目标集群模型对齐
# MU_AGENTTEAMS_TIMEOUT_MS=120000             # 可选；轮询/请求超时上限（≤300000）
```

正式路径同时要求 Matrix 任务传输层（Wave 3.4 —— 与上游 delegate 同协议的 worker room 消息通道；缺配置 fail-closed `MT_NOT_CONFIGURED`）：

```bash
MU_AGENTTEAMS_MATRIX_URL=http://<agentteams-controller>:6167   # Matrix CS API（见 deploy/agentteams-beta/RUNBOOK.md §10）
MU_AGENTTEAMS_MATRIX_USER=<AgentTeams admin 用户>
MU_AGENTTEAMS_MATRIX_PASSWORD=<部署级凭据（进程环境注入，不入库/日志）>
```

internal 执行器仅限开发/测试/应急路径，必须同时显式声明降级开关（缺失即拒绝）：

```bash
MU_EXECUTOR=internal
MU_EXECUTOR_INTERNAL_ALLOW=development        # 三选一：development | test | emergency
```

其他任何值（含未设置）→ 执行器门拒绝（`EXECUTOR_MODE_UNSET` / `EXECUTOR_MODE_INVALID` / `EXECUTOR_INTERNAL_NOT_ALLOWED`），审计记 `executor_gate_rejected`。前端与 `/api/mu/agent-policy` 展示当前执行器模式。

### RAG 查询 scope（用于本地 RAG 试用）

```bash
RAGTRIAL_ALLOWED_SCOPES=<owner>/<repo>@<branch>
RAGTRIAL_REPO_ALLOWLIST=<owner>/<repo>
```

### Private Key 安全注入

**方式一（推荐）**：直接粘贴到 `.env`（多行值用引号包裹）：
```bash
MU_GITHUB_APP_PRIVATE_KEY="<your-private-key-content>  # .pem 文件全部内容，含 BEGIN/END 行
```

**方式二**：启动脚本从文件读取注入（参见 `deploy/local-rag-trial/` 的 compose 环境变量传递）。

> 两种方式均不入库、不进日志、不进审计。数据库和审计中零私钥/token 存储。

## 6. 首次登录与邀请用户

### 6.1 首次登录（Bootstrap 管理员）

首次启动时系统自动创建迁移租户（`default`）和一个 **fixture 引导操作员**（默认登录名
`pilot-admin`，provider=`fixture`，仅下述方式 B 可登录；生产保持关闭）。

> **首个真实用户与首个平台管理员**：GitHub OAuth 登录只接受**事先创建的邀请**（无邀请即
> `not_invited`，无公共自动注册），且邀请**永不授予 `platform_admin`**（API/claim/DB 三层拒绝）。
> 因此生产部署的首个平台管理员由部署者**数据库直授**——完整的身份定位 / 最小授权 / 验证 / 回退
> 步骤见 [`deploy/selfhost/README.md`「首个平台管理员初始化（生产）」](../deploy/selfhost/README.md#首个平台管理员初始化生产)。
> 邀请仅用于其余成员与角色（见 §6.3）。

**方式 A：GitHub OAuth 登录**（受邀成员；需配置 §5 OAuth）

1. 访问 `http://<你的地址>/multiuser`
2. 点击 **使用 GitHub 登录**
3. GitHub 授权后自动回跳

**方式 B：Fixture 登录**（仅测试/开发，默认关闭）

需显式设置 `MU_ALLOW_FIXTURE_LOGIN=1`：
```bash
# .env 中添加
MU_ALLOW_FIXTURE_LOGIN=1
```
然后以 fixture 身份登录。

> ⚠️ 生产部署**不要**设置 `MU_ALLOW_FIXTURE_LOGIN=1`。

### 6.2 角色说明

| 角色 | 权限 | 典型使用者 |
|---|---|---|
| **Contributor** | 查看 PR/仓库元数据/代码内容；RAG 检索 | 开发者 |
| **Reviewer** | Contributor 全部 + 触发审查 + finding 反馈 | 安全审查员 |
| **Maintainer** | Reviewer 全部 + **审批修复票/PR 裁定/发布评论/应用修复/确认合并**（decide_review/apply_fix/merge_pr 仅此角色）+ 绑定/解绑仓库 + 发起修复 | 团队负责人 |
| **PlatformAdmin** | 管理成员/租户/实例配置/出站策略（**刻意无审批与发布权、无代码内容读取**——平台管理与裁定分离） | 运维管理员 |
| **Auditor** | 仅审计元数据只读（不可读代码/RAG 内容） | 合规审计员 |

> 完整矩阵见 [docs/delivery/CAPABILITY-MATRIX.md §2](delivery/CAPABILITY-MATRIX.md)。

### 6.3 邀请用户

1. 以 PlatformAdmin 或 Maintainer 登录
2. `POST /api/mu/invitations`（或经控制台 UI）：

```bash
curl -X POST http://<你的地址>/api/mu/invitations \
  -H "Content-Type: application/json" \
  -H "Cookie: <session>" -H "X-CSRF-Token: <csrf>" \
  -d '{
    "expected_subject": "<被邀请者的 GitHub 数字 user id>",
    "role": "reviewer",
    "ttl_minutes": 1440
  }'
```

> **必须绑定 GitHub 数字 user id**（`expected_subject`），不能仅用 login 句柄（handle 可被夺注）。
> 获取数字 id：让被邀请者访问 `https://api.github.com/users/<login>` 并复制 `id` 字段。

3. 被邀请者用 GitHub OAuth 登录后自动认领邀请并获得角色。

### 6.4 邀请生命周期

- **短期**：默认 24 小时有效期（`ttl_minutes` 最大 1440）
- **单次**：认领后不可复用（CAS 原子操作）
- **过期**：过期后不可认领，需重新创建

## 7. 仓库绑定与 PR 审查流程

### 7.1 安装 GitHub App 到你的账号

1. 以 **Maintainer** 登录 `/multiuser`
2. 在 **GitHub App** 面板点击 **安装 / 更新 GitHub App**
3. 浏览器跳转到 GitHub → 选择安装目标（账号/组织）→ 授权仓库
4. 安装完成后自动回跳到 `/multiuser`
5. Console 登记 installation（绑定到当前租户）

### 7.2 绑定仓库

1. 在 GitHub App 面板查看 installation 列表
2. 选择 installation → 查看授权仓库列表（**由 GitHub API 服务端返回**，不信任客户端）
3. 选择要绑定的仓库 → 绑定

```bash
# API 方式
curl -X POST http://<你的地址>/api/mu/repositories/<repo_id>/ghapp-binding \
  -H "Content-Type: application/json" \
  -H "Cookie: <session>" -H "X-CSRF-Token: <csrf>" \
  -d '{"installation_id": <id>, "github_repo_id": <numeric_id>}'
```

> 一个 GitHub 仓库默认只能被一个租户绑定（DB 全局唯一约束）。

### 7.3 PR 审查流程

1. 绑定仓库后，PR 事件（新开/更新）自动触发审查（webhook → 验签去重 → 入队 → 自动消费）
2. Worker 写入 PR 快照（tenant/repo/head_sha）→ 静态规则审查（`source=precheck` 发现落库）
3. 若部署启用了 LLM 第二阶段（§5 LLM env + 控制台运行策略 + 出站策略/同意）：模型审查
   **无条件执行**（与静态结果解耦），模型发现以 `source=reviewer` 独立标注；失败 fail-closed
   回落静态结果并如实记档，绝不误报"模型已执行"
4. Reviewer / Maintainer / PlatformAdmin 可触发审查 / 提交 finding 反馈（`request_review`）；仅 Maintainer 可审批或驳回（`decide_review`）
5. P0/P1 发现自动生成具名审批票；run 内全部批准 → 修复轮（dry-run）→ 补丁版本化 + 验证三分
6. Maintainer 可在产物面板「预览并发布到 PR」把审查结果发布为机器人评论
   （远端 head 复核 + 幂等原位更新；见 [USER-GUIDE](delivery/USER-GUIDE.md)）
7. Maintainer 还可逐次确认**应用修复**（机器人向 PR 分支提交修复 commit → 新 head 自动
   重审 → 该 head 真实 CI 判定）与**确认合并**（统一资格门：head 一致 + mergeable 已知 +
   当前 head 审查完成无未处理 P0/P1 + 当前 head CI 通过）；确认必须从预览发起，
   绑定远端 head 与补丁 digest；结果不明（`result_uncertain`）时以「读回对账」定案，
   禁止盲目重试。完整步骤与阻塞处理见 [USER-GUIDE §3](delivery/USER-GUIDE.md)

> **branch protection 状态未知时不可审批**（`422 cannot_conclude_mergeable`）。

### 7.4 解绑

```bash
curl -X DELETE http://<你的地址>/api/mu/repositories/<repo_id>/ghapp-binding \
  -H "Cookie: <session>" -H "X-CSRF-Token: <csrf>"
```

### 7.5 卸载 App 的级联效果

在 GitHub 卸载 App → webhook `installation deleted` → Console 自动：
- installation 进入 `revoked` 状态
- 所有关联 binding 进入 `revoked` 状态
- 后续 webhook 和 worker 写入被拒绝

## 8. 本地 RAG 增强启用

### 资源要求

| 档位 | 配置 | 嵌入策略 |
|---|---|---|
| 基础 | 2C2G | `local-hash-v1`（零模型下载） |
| 推荐 | 4C8G | `local-hash-v1` 或 `bge-m3`（需自带模型工件） |
| 高配 | 8C16G | `bge-m3`（8C16G 档） |

### 启用步骤

1. 配置 `RAGTRIAL_ALLOWED_SCOPES`（部署级 scope 门——定义可检索的 repo@branch 范围，fail-closed：未设置=全拒绝 403 `scope_not_configured`）
   ```bash
   # .env 示例：允许检索你绑定的仓库
   RAGTRIAL_ALLOWED_SCOPES=your-owner/your-repo@main
   # 多个用逗号分隔
   RAGTRIAL_ALLOWED_SCOPES=owner/repo-a@main,owner/repo-b@dev
   ```
2. 配置 `RAGTRIAL_REPO_ALLOWLIST`（会话端仓库授权面——与部署 scope 取交集后才是有效授权）
   ```bash
   RAGTRIAL_REPO_ALLOWLIST=your-owner/your-repo
   ```
   有效授权 = `RAGTRIAL_ALLOWED_SCOPES` ∩ `RAGTRIAL_REPO_ALLOWLIST`，两层缺一不可。请求参数/body 只能被校验，不能扩大该交集。

3. 通过 `/api/rag-trial/ingest` 导入语料
4. 通过 `/api/rag-trial/query` 检索

详见 [LOCAL-RAG-GUIDE](../distribution/docs/LOCAL-RAG-GUIDE.md)

### 多用户 RAG 会话

- **multiuser 模式下 RAG 端点支持 `mu_session` 持久化会话**（`/api/rag-trial/*` 同时识别 legacy `mp_session` 和 MU `mu_session`）
- 逐请求检查：session 有效 + 用户 active + 成员关系 active + 角色 `rag_query` 动作 + tenant 仓库列表（全部服务端）
- **Auditor 和 PlatformAdmin 默认无 `rag_query` 动作 → RAG 401**（角色矩阵）
- 成员撤销后既有 session **立即失权**（逐请求 membership live 检查）

### hash 嵌入与 `state=empty`

默认嵌入策略 `local-hash-v1` 是确定性哈希（非语义），对**短中文查询可能返回 `state=empty`**（无精确词匹配）——这是"无结果"而非"认证失败"或"权限拒绝"。如需语义检索能力，请配置 `bge-m3`（8C16G 档，需自带模型工件 + manifest 校验，详见 [LOCAL-RAG-GUIDE](../distribution/docs/LOCAL-RAG-GUIDE.md#嵌入策略)）。

### RAG 安全红线

- 检索结果**仅作 reference**，不自动生成 finding/ticket/gate/VERIFIED
- 不参与风险决策
- 语义模型须自带并过 manifest 校验（零下载）

## 8.1 测试后清理（GitHub App 卸载与凭据清理）

真实测试完成后请按以下步骤清理：

1. **卸载 GitHub App**：https://github.com/settings/installations → 找到测试 App → Uninstall
   - Console 自动收到 `installation deleted` webhook → installation 进入 `revoked` + 所有关联 binding 进入 `revoked`
   - 后续 webhook 和 worker 写入被拒绝（fail-closed）
2. **删除测试 App**（可选，如不再需要）：https://github.com/settings/apps/{app-name} → Delete GitHub App
3. **清理本地凭据**：
   ```bash
   rm .env                          # 含 App ID/private key/webhook secret
   rm ~/Downloads/*.private-key.pem # GitHub App 私钥文件
   docker compose down -v           # 删除所有数据（慎重）
   ```
4. **验证清理**：`curl http://<你的地址>/api/mu/github/app/status` 应返回 `configured: false`

> **注意**：以上真实 E2E 验证均使用测试 App 和测试仓库（非生产环境），不代表你的部署环境无需配置验证。生产部署请按 §4-§5 重新配置并验证。

## 9. FXV 修复验证

> **位置说明**：multiuser 修复链的运行面由 `MU_EXECUTOR` 决定（生产=agentteams，见 §5 执行器与
> [DEPLOY-GUIDE 目标三](delivery/DEPLOY-GUIDE.md)）；FXV 是修复/验证子进程执行底座
> （fixer/verifier worker，隔离临时工作树）。远端写入门仅属 FXV 直连 GitHub 的历史路径。

### 默认行为

- **Dry-run 默认开启**（`FXV_DRY_RUN=1`）
- Fixer/Verifier 在隔离环境中运行（真子进程 + 真 git）
- **GitHub 写入默认关闭**（`FXV_GITHUB_WRITE=disabled`）

### 启用真实写入（不推荐在 Beta 使用）

需同时满足三个条件：
1. `FXV_DRY_RUN=0`
2. `FXV_GITHUB_WRITE=authorized`
3. 存在有效的 `fxv.grants` 授权

详见 [FIXER-VERIFIER](../distribution/docs/FIXER-VERIFIER.md)

## 10. 备份、升级、回滚与卸载

### 备份

```bash
# PG 数据
docker exec <pg-container> pg_dump -U postgres ragtrial > backup_$(date +%Y%m%d).sql

# MinIO 数据卷
docker run --rm -v <minio-volume>:/data -v $(pwd):/backup alpine tar czf /backup/minio-data.tar.gz /data
```

### 升级

1. 备份（如上）
2. 拉取新镜像或重新构建
3. `docker compose up -d`（mu schema migration 自动执行，幂等）

### 回滚

```bash
# 镜像回滚
docker tag <旧镜像> <当前标签>
docker compose up -d

# 数据回滚
docker exec -i <pg-container> psql -U postgres ragtrial < backup_YYYYMMDD.sql
```

### 卸载

```bash
docker compose down -v   # 删除所有数据（慎重！）
```

## 11. 常见故障排查

### GitHub App 未配置

```
GET /api/mu/github/app/status → {"configured": false, "reason": "github_app_not_configured"}
```
→ 检查 `MU_GITHUB_APP_ID/_APP_SLUG/_PRIVATE_KEY/_WEBHOOK_SECRET/_INSTALL_CALLBACK_URL` 五项是否全部设置。

### Webhook 未到达

1. 检查 GitHub App 设置中 Webhook URL 是否正确
2. 检查 webhook secret 是否与 `MU_GITHUB_WEBHOOK_SECRET` 一致
3. 检查 console 是否可从公网访问（`/api/health` 须返回 200）
4. 查看 GitHub App → Advanced → Recent Deliveries

### 邀请认领失败（`not_invited`）

→ 确认 `expected_subject` 使用的是 **GitHub 数字 user id**（不是 login 句柄）。
获取方式：`https://api.github.com/users/<login>` 的 `id` 字段。

### 登录/邀请常见错误速查

| 提示 | 含义与处理 |
|---|---|
| `invitation_not_found` | 邀请 id 不存在、链接被篡改或已删除——向管理员索取新链接 |
| `invitation_expired` | 邀请超过有效期——请管理员重建邀请 |
| `invitation_already_claimed` | 邀请已被领取（单次认领）——如需再次加入请联系管理员 |
| `invitation_ambiguous` | 同一 GitHub 数字 id 存在多条待认领邀请——管理员清理重复邀请后重试 |
| `fixture_login_disabled` | fixture 操作员登录未开启——生产保持关闭，属预期安全行为 |
| `legacy_login_disabled_in_multiuser` | multiuser 形态禁用旧操作员密码登录——使用 GitHub OAuth |

### 登录失败（`legacy_login_disabled_in_multiuser`）

→ 多用户模式下禁止共享操作员账号。使用 `/api/mu/auth/login`（OAuth 或 fixture）。

### 会话丢失（重启后 401）

→ DB 持久会话在 PG 重启后自动恢复。如 PG 数据卷被删除，需重新登录。

### PR 审查未触发

→ 确认：① 仓库已绑定且 binding_state=active；② GitHub App 已订阅 pull_request 事件；③ webhook URL 可达。

## 12. Beta 已知限制

| 限制 | 说明 |
|---|---|
| **无 RLS** | 应用层租户约束，非 PostgreSQL 行级隔离 |
| **无 SSO/SCIM** | 仅 GitHub OAuth + 邀请制 |
| **无 HA** | Docker Compose 单机部署 |
| **无配额限流** | 不限制用户请求数 |
| **无自动 approve/merge** | 合并=人工逐次确认（`merge_pr` 资格门）；不存在任何自动批准/自动合并路径 |
| **仓库写入仅限受控两类** | 机器人提交修复（`apply_fix`）与合并（`merge_pr`）均需维护者逐次「预览 → 确认」+ 对应 App 写权限；缺权限时界面如实报告 unsupported，不用 PAT 绕过 |
| **Gitee CI 读取不支持** | 当前 Gitee 连接器读不到 CI 状态（连接器能力边界）→ Gitee 仓库的合并被资格门恒阻塞 |
| **会话清扫** | 过期会话行无定时清理（行量低时可忽略） |
| **单 webhook secret** | 全局一个（GitHub App 标准做法） |

## 13. 安全声明

- **凭据红线**：private key / webhook secret / OAuth secret 仅通过环境变量注入，绝不入数据库、日志或审计
- **DB 零明文**：session token、CSRF、OAuth state、correlation cookie 均只存 sha256 摘要
- **Cookie 安全**：HttpOnly + SameSite=Lax + Path=/，生产模式（`NODE_ENV=production`）强制 Secure
- **CSRF 防护**：双提交（可读 cookie + 请求头 timing-safe 比较）
- **审计完整性**：白名单制（未知 kind 一律拒绝）、tenant/platform 域分立、脱敏（不含查询正文/凭据/allowlist 原值）
- **最小权限**：审查只需 6 项只读权限；仅启用应用/合并才追加 Contents/Pull requests 读写；零 Administration:write / Members / Actions，缺权限如实报告而非用 PAT 绕过
- **邀请制**：无公共自动注册，无首访即管理员

## 14. 反馈方式

- **Bug 报告 / 功能建议**：[GitHub Issues](https://github.com/nghqqa/MergePilot/issues)
- **安全漏洞**：请勿公开 Issue，参见 [SECURITY.md](../distribution/docs/SECURITY.md)
- **贡献代码**：参见 [CONTRIBUTING.md](../CONTRIBUTING.md)
