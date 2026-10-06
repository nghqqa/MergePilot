# MergePilot rc.15 自托管部署

> 版本：`0.2.0-beta.6-rc.15` · 镜像 digest：`sha256:f0b30fd1b0b366fc4d203848b657f6cc9012bef8e45459ba7c099fd1a13897b3`
> 官方镜像已在 GHCR 发布（不可变 digest）。离线 tar 与源码构建为等效替代途径，校验方式见下。
> 供应链档案：SBOM（CycloneDX，35 组件）、trivy 扫描（漏洞 0/秘密 0）、离线 tar SHA256 与源码 tag 见「镜像获取与校验」。

## 快速开始

```bash
cp .env.example .env          # 按注释填写必填段；chmod 600 .env
node preflight.mjs            # 一键只读检查（关键项失败即退出 1）
docker compose --env-file .env up -d
node preflight.mjs --live     # 启动后活体探测（health/schema/queue）
curl http://127.0.0.1:48500/api/health
```

首次启动自动完成 schema 初始化（至 v23）；随后访问 `http://127.0.0.1:48500/multiuser`
完成 GitHub OAuth 登录（首个用户需先经邀请，见「多租户与 webhook」）。

## 镜像获取与校验

| 途径 | 命令 | 校验 |
|---|---|---|
| GHCR（推送生效后） | `docker pull ghcr.io/nghqqa/mergepilot-console@sha256:f0b30fd1…3897` | RepoDigest == 该 digest |
| 离线 tar | `docker load -i mergepilot-console-rc15.tar` | tar SHA256 = `6d1341f7f289318be986dc8aae16e90e122bcd41b897cf0ed312a2c60ecd6750`；load 后 image ID = `f0b30fd1…` |
| 源码构建 | 见下节 | image ID 应可复现（同 commit/同构建参数） |

镜像内置 `MERGEPILOT_VERSION=0.2.0-beta.6-rc.15`（`/api/health` version 字段即真源）。

**供应链档案（rc.15）**：

- SBOM：CycloneDX 1.5，**35 组件**（`rc15-sbom.cdx.json`，随发行版发布）；
- 漏洞/秘密扫描（trivy 0.74，ghcr.io 官方漏洞库）：**vulnerabilities=0、secrets=0**；
- 构建来源：源码 tag `v0.2.0-beta.6-rc.15` = commit `e660a5aa9a`（与镜像 digest `f0b30fd1…3897` 同源）；
- 基础镜像：`node@sha256:0a7108bf…`（Dockerfile 钉死）。

> `48590` 端口与 `beta-webhook-proxy` 容器是 MergePilot 开发方内部测试环境的组件，
> **不属于自托管发行版，也不是官方托管服务入口**。自托管部署的对外入口是
> 你自己的 TLS 反向代理（见安全加固清单）。

## 从源码构建

```bash
git checkout v0.2.0-beta.6-rc.15        # 源码 tag（与镜像同源）
cd console/frontend && npm ci && npm run build && cd ../..
docker build -f docker/Dockerfile.canonical-console \
  --build-arg MERGEPILOT_VERSION=0.2.0-beta.6-rc.15 \
  -t ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.15 .
```

注意两点：① 构建前必须先产出 `console/frontend/dist`（Dockerfile 会 COPY 它）；
② `--build-arg MERGEPILOT_VERSION` 决定 `/api/health` 的 version 字段。

## 配置参考

全部变量见 `.env.example`（含生成命令与三类标注：必填 / LLM 必填 / 可选）。
要点：

- `POSTGRES_PASSWORD`：compose 拼接 DSN，不出现在其他文件；
- `CONSOLE_SESSION_SECRET`：会话签名，`openssl rand -base64 32`；
- GitHub App + OAuth App：**两个都要建**（前者收 webhook，后者做用户登录）；
- cchain 三键（`MERGEPILOT_MODEL_CACHE_DIR` / `MERGEPILOT_PROVIDER_ATTEST_URL` /
  `MERGEPILOT_RUN_BINDING_KEYSTORE`）：可选，三项不全时 cchain=BLOCKED（fail-closed 如实呈现）；
- cchain 引导（首次转 READY 需三步，缺一会 BLOCKED）：
  ① 模型目录放入 bge-m3 文件与安装流程生成的 `manifest.json`（sidecar 按 `BGE_MANIFEST` 校验）；
  ② `fxv.audit_events` 表需初始化（cchain 审计真写的前提）——
     `CREATE SCHEMA IF NOT EXISTS fxv; CREATE TABLE IF NOT EXISTS fxv.audit_events
     (id bigserial PRIMARY KEY, attempt_id text, kind text NOT NULL, from_state text,
      to_state text, actor text NOT NULL, reason text, meta jsonb,
      created_at timestamptz NOT NULL DEFAULT now());`
  ③ keystore 首把种子密钥（rotate 拒绝空 keystore——鸡生蛋由种子打破）：
     生成 `{"key_id":"rk-bootstrap-1","secret":"<openssl rand -hex 32>","created_at":"<ISO>",
     "expires_at":"<+90天 ISO>","revoked":false}` 写入 keystore 卷，**属主与权限须匹配容器
     运行用户（uid 1000:1000，目录 770/文件 600）**；随后经 rotate API 轮换出正式在役密钥；
- RAG 四键：可选，不填则 RAG=NOT_WIRED 如实降级。

## 多租户与 webhook

1. **GitHub App**：按最小权限创建（Metadata/Contents/Pull requests/Checks/Statuses 全只读；
   不申请任何 write）+ 订阅五种事件（installation、installation_repositories、pull_request、
   check_run、status）+ webhook URL 指向你的 ingress（默认 `http://<host>:48590/api/mu/github/webhook`，
   对外部署请换成 https 域名）；**务必配置与 `MU_GITHUB_WEBHOOK_SECRET` 一致的签名密钥**。
2. **installation 注册**：安装 App 后跳转回控制台即注册到当前会话所属租户；
   租户成员的邀请认领（rc.14 起泛化、rc.15 持续）支持既有用户经邀请进入其他租户；
   同一 subject 存在多条待认领邀请时会显式拒绝（`invitation_ambiguous`）。
3. **隔离语义**：事件租户跟 installation 走；八张业务表按 tenant_id 强制收窄；
   撤权后同会话下一请求即 403；`platform_admin` 永不可经邀请授予（API/claim/DB 三层拒绝）。

## 升级与回滚

```bash
# 升级：改 compose 里 console 镜像引用（digest 或版本 tag）后
docker compose --env-file .env pull console && docker compose --env-file .env up -d
# 回滚：把引用改回旧 digest 再 up -d。数据卷不动。
```

**schema 只前进不降级**：新版可能自动前移 `mu.schema_migrations`；回滚镜像后旧代码
对新列/新表无感知（历史兼容已验证），但**不可跨过 schema 破坏性操作**——升级前
`pg_dump` 一份（见下）。全部历史迁移为 additive（rc.5→rc.15 实证）。

## 备份

```bash
docker exec mergepilot-postgres-1 pg_dump -U postgres -d mu -Fc -f /tmp/mu.backup
docker cp mergepilot-postgres-1:/tmp/mu.backup ./mu-$(date +%F).backup
# 恢复：docker exec -i mergepilot-postgres-1 pg_restore -U postgres -d mu --clean < mu.backup
```

## 安全加固清单

- 对外暴露**必须**前置 TLS 反代——强制，非建议；明文 HTTP 不允许承载已认证会话。Caddy 示例：`reverse_proxy 127.0.0.1:48500`（自动证书）。
  保持 console/webhook-ingress 端口绑定 127.0.0.1；
- 防火墙仅放行反代端口；数据库/keystore/模型卷不对外；
- `.env` 与私钥文件权限 0600；secrets 不入 git、不入日志（日志层无 secrets 输出为设计合同）；
- 入口限流建议：反代层对 `/api/*` 限 60 req/min/IP，webhook 路径 120 req/min/IP；
- 定期轮换：`CONSOLE_SESSION_SECRET`（会话全失效，用户重登）、keystore（rotate API，旧 key 即刻失效）；
- webhook 端点有 HMAC 签名 + delivery 一次性去重；管理面全部要求会话 + CSRF。

## 故障排查

| 症状 | 排查 |
|---|---|
| `/api/health` 无响应 | `docker compose ps` 看 console 是否 healthy；`docker compose logs console` |
| version 不对 | 检查 `--build-arg MERGEPILOT_VERSION` / env 覆盖（镜像内置为真源） |
| schema 未就绪 | `docker compose logs postgres`；确认 pgdata 卷未被旧实例占用 |
| webhook 无投递 | DNS/防火墙/反代链路；GitHub App 高级页看 Recent Deliveries 状态与 HMAC 结果 |
| 登录报 not_invited | 该 GitHub 账号需先由管理员按其**数字 user id** 创建邀请 |
| cchain=BLOCKED | 三键是否齐全；attestation 端点可达性；keystore 是否已 rotate 出在役 key |
| 登录后 403 membership_inactive | 会话绑定租户的成员关系已被撤——重新登录会落到其余 active 租户 |

## 已知限制（rc.15）

- 无租户切换端点/UI（多租户用户以对应租户邀请重新登录）；
- 私有 GitHub App 仅所有者账号可安装；多租户 webhook 需公开化 App 或每租户独立 App；
- webhook ingress 仅放行 webhook 与 health 两个路径（管理面经 48500 直连或反代）。
