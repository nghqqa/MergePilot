# MergePilot Developer Edition Beta — 完整安装与配置指南

本文档覆盖从零到完整使用的全部步骤，包括 GitHub App 创建、凭据注入、首次登录、邀请用户、仓库绑定和 PR 审查。

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

- 通过 GitHub App **只读**接入你的仓库
- 自动同步 Pull Request（webhook 驱动）
- 运行安全审查并产出结构化发现
- 管理修复验证流程（dry-run 默认）
- 全程带完整审计追踪（tenant 域 + platform 域分立）
- 可选开启本地 RAG 增强（语料库检索为审查提供可引用的参考证据）

**核心理念**：数据不出你的机器；GitHub App 权限最小化（仅 read-only）；不自动 merge、不自动 approve。

## 2. Developer Edition Beta 与 Enterprise 的边界

| 能力 | Developer Beta（当前） | Enterprise（未实现） |
|---|---|---|
| 多用户 RBAC | ✅ 五角色应用层 | RLS 行级隔离 |
| 身份 | GitHub OAuth + 邀请制 | SSO (SAML/OIDC) + SCIM |
| 仓库接入 | GitHub App read-only | + 写权限（独立设计） |
| 会话 | DB 持久（单机） | HA + 集中密钥管理 |
| 配额限流 | ❌ | ✅ |
| 自动 approve/merge | ❌ 设计禁止 | 受控合并（独立设计） |

**当前版本不是 Enterprise Production Ready。**

## 3. Docker Compose 快速启动

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
| Webhook URL | `http://<你的地址>/api/mu/github/webhook` |
| Webhook secret | 自定义随机字符串（如 `openssl rand -hex 32`） |

### 4.2 权限（仅勾这 5 项，全部 Read-only）

| 权限 | 级别 |
|---|---|
| Metadata | **Read-only** |
| Contents | **Read-only** |
| Pull requests | **Read-only** |
| Checks | **Read-only** |
| Commit statuses | **Read-only** |

> ⚠️ **不勾任何 Write / Administration / Members / Actions 权限。**

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

### 会话与安全（可选覆盖）

```bash
MU_SESSION_TTL_MS=28800000      # 会话 TTL（默认 8h）
MU_OAUTH_FLOW_TTL_MS=600000     # OAuth state 有效期（默认 10min）
```

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

首次启动时系统自动创建迁移租户（`default`）和 Bootstrap 管理员（角色=PlatformAdmin）。

**方式 A：GitHub OAuth 登录**（推荐，需配置 §5 OAuth）

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
| **Contributor** | 查看 PR/仓库元数据/证据；RAG 检索 | 开发者 |
| **Reviewer** | Contributor 全部 + 触发只读审查 | 安全审查员 |
| **Maintainer** | Reviewer 全部 + 审批 + 绑定/解绑仓库 + 发起修复 | 团队负责人 |
| **PlatformAdmin** | 管理成员/租户/实例配置（不自动获得代码读取） | 运维管理员 |
| **Auditor** | 仅审计元数据只读（不可读代码/RAG 内容） | 合规审计员 |

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

1. 绑定仓库后，GitHub 自动发送 `pull_request` webhook
2. Console 验签（HMAC-SHA256 + 常量时间比较）→ 去重（delivery_id）→ 入队
3. Worker 消费 `event_sync` job → 写入 PR 快照（tenant/repo/head_sha）
4. Reviewer 可触发只读审查（`POST /api/mu/prs/<id>/review`）
5. Maintainer 可审批或驳回（`POST /api/mu/prs/<id>/decision`）

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

1. 配置 `RAGTRIAL_ALLOWED_SCOPES`（定义可检索的 repo@branch 范围）
2. 配置 `RAGTRIAL_REPO_ALLOWLIST`（定义会话端可见仓库）
3. 通过 `/api/rag-trial/ingest` 导入语料
4. 通过 `/api/rag-trial/query` 检索

详见 [LOCAL-RAG-GUIDE](../distribution/docs/LOCAL-RAG-GUIDE.md)

### RAG 安全红线

- 检索结果**仅作 reference**，不自动生成 finding/ticket/gate/VERIFIED
- 不参与风险决策
- 语义模型须自带并过 manifest 校验（零下载）

## 9. FXV 修复验证

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
| **无自动 approve/merge** | 设计禁止，结构性不存在代码路径 |
| **无仓库写入** | GitHub App 仅 read-only |
| **会话清扫** | 过期会话行无定时清理（行量低时可忽略） |
| **单 webhook secret** | 全局一个（GitHub App 标准做法） |

## 13. 安全声明

- **凭据红线**：private key / webhook secret / OAuth secret 仅通过环境变量注入，绝不入数据库、日志或审计
- **DB 零明文**：session token、CSRF、OAuth state、correlation cookie 均只存 sha256 摘要
- **Cookie 安全**：HttpOnly + SameSite=Lax + Path=/，生产模式（`NODE_ENV=production`）强制 Secure
- **CSRF 防护**：双提交（可读 cookie + 请求头 timing-safe 比较）
- **审计完整性**：白名单制（未知 kind 一律拒绝）、tenant/platform 域分立、脱敏（不含查询正文/凭据/allowlist 原值）
- **最小权限**：GitHub App 仅 5 项 read-only 权限，零 write/administration/actions
- **邀请制**：无公共自动注册，无首访即管理员

## 14. 反馈方式

- **Bug 报告 / 功能建议**：[GitHub Issues](https://github.com/nghqqa/MergePilot/issues)
- **安全漏洞**：请勿公开 Issue，参见 [SECURITY.md](../distribution/docs/SECURITY.md)
- **贡献代码**：参见 [CONTRIBUTING.md](../CONTRIBUTING.md)
