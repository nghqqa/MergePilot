# MergePilot rc.19 自托管部署（候选）

> 版本：`0.2.0-beta.6-rc.19` · 镜像 pull digest（OCI index）：`sha256:0077382db28d454e7e03bbbd9ad3c2a1ddcd97cf84bc4cabe81c4d2b77610ad4`
> index 内含 linux/amd64 manifest `sha256:413dbd34f43b148f6007be3fbdb3d7d4ccbd7f6fe7a2fe652cb7954704edd740`、config digest `sha256:441ec6892abf69d46cc169de8bce49a9a0c2b7b692aef7795e054257bf4bb20d`（三者不同对象，勿混称）。
> 已正式发布 GHCR（不可变 digest）。离线 tar 与源码构建为等效替代途径，校验方式见下。
> 离线 tar 与源码构建为等效替代途径，校验方式见下。
> 供应链档案：SBOM（CycloneDX）、trivy 扫描、离线 tar SHA256 见「镜像获取与校验」（哈希随候选制品提供）。
> 相对 rc.18：**零 schema/迁移变更（恒 v23）**，品牌区去 V0 标签、移动抽屉品牌可读性修复、运维工具输出安全化与 AgentTeams 认证代理运维交付。

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

## 部署后首次使用

### 这是什么形态的产品（先读）

MergePilot 是**用户自托管**产品：Console、数据库、OAuth 配置和业务数据全部运行在
**部署者自己的环境**中，MergePilot 项目方不托管任何用户的 Console 或数据。

| 部署形态 | 能做什么 | 不能做什么 |
|---|---|---|
| **本机 / 内网**（127.0.0.1 / 局域网） | 本机 GitHub OAuth 登录、本地评估、受限使用 | 跨机器邀请、自动接收 GitHub Webhook |
| **公网 HTTPS**（部署者自有域名 + 反代） | 上述全部 + GitHub Webhook 自动进入管线 + 跨机器邀请认领 | 需要部署者自己运营公网入口与 TLS |

GHCR 镜像包为 **Public** 仅表示自托管用户可以匿名拉取镜像；**不代表 MergePilot
提供托管版 Console，也不会自动为部署者提供公网入口**——公网入口永远由部署者
自己的反代/隧道与域名构成。

### Console 登录地址

| 场景 | 地址 |
|---|---|
| 本机访问 | `http://127.0.0.1:48500/login`（或 `/multiuser`） |
| 公网 | `https://<console 公开地址>/login`（经部署者自己的 HTTPS 反代） |

登录方式：**GitHub OAuth**（仅受邀成员）。生产 multiuser 形态下不提供操作员密码
表单，也不提供演示入口（见下「登录与演示的默认关闭项」）。

### GitHub OAuth callback 格式

在 GitHub OAuth App 中填写（**必须与实际访问地址同源**）：

```text
<Console 公开地址>/api/mu/auth/oauth/github/callback
# 本机评估：http://127.0.0.1:48500/api/mu/auth/oauth/github/callback
# 公网生产：https://<console 公开地址>/api/mu/auth/oauth/github/callback
```

### Webhook 对外地址要求

GitHub App 的 Webhook URL 必须是 **公网可达的 HTTPS 地址**，转发到本机
`48590`（webhook-ingress）。127.0.0.1/localhost **不可作为 GitHub Webhook 的
对外地址**（GitHub 服务器无法访问你的回环地址）；没有公网入口时，本地管理
界面可用，但 webhook 无法自动进入审查管线（详见「多租户与 webhook」）。

### 管理员创建邀请（唯一准入通道）

管理员登录后进入 **组织与接入 → 成员与邀请** 面板：

1. 填写受邀人的 **GitHub 数字 user id**（必填；`https://api.github.com/users/<login>` 的 `id` 字段）、
   角色（maintainer / reviewer / contributor / auditor）、有效期（默认 1440 分钟）；
2. 点击 **创建邀请** → 面板给出 **一键复制邀请链接**：

```text
https://<console 公开地址>/login?invite=<invite_id>
```

3. 邀请语义：绑定 **GitHub 数字 id + 租户 + 角色 + 有效期 + 单次认领**；**邀请永不授予
   `platform_admin`**（API/认领守卫/数据库 CHECK 三层拒绝）。链接只是入口，**不是
   可转让的授权凭证**——认领时后端校验到访者 GitHub 身份与绑定 id 一致才生效。

### callback 未配置或为回环地址时

成员与邀请面板会按 OAuth callback 地址**自动标注链接适用范围**：回环（127.0.0.1/
localhost）→ **「仅本机可用」**；HTTP 非回环 → 「不符合推荐的对外 HTTPS 配置（内网
适用）」；HTTPS 非回环 → 可共享（仅证明格式符合要求，不探测公网可达性）。callback
缺失时不生成链接，只提示先配置 `MU_GITHUB_OAUTH_CALLBACK_URL`。

### 如何验证三条链路

| 链路 | 验证方法 |
|---|---|
| 登录 | 打开 Console 登录地址 → 点击 GitHub 登录 → 落回工作台且右上角显示已登录身份 |
| Webhook | GitHub App 高级页 **Recent Deliveries** 出现 2xx（可用 Redeliver 重放历史投递验证端到端；无公网入口时恒为空/失败——属预期） |
| 邀请 | 管理员复制邀请链接 → 受邀人用自己的 GitHub 账号打开 → 登录后成员面板出现对应租户与角色 |

### 常见错误速查

| 页面/API 提示 | 含义与处理 |
|---|---|
| `not_invited` / 身份未被邀请 | 到访者 GitHub 数字 id 没有对应邀请——管理员按数字 id 创建邀请 |
| `invitation_not_found` | 邀请 id 不存在、链接被篡改或已过期删除——向管理员索取新链接 |
| `invitation_expired` | 邀请超过有效期——请管理员重建邀请 |
| `invitation_already_claimed` | 邀请已被领取（单次认领）——如需再次加入请联系管理员 |
| `invitation_ambiguous` | 同一 GitHub id 存在多条待认领邀请——管理员清理重复邀请后重试 |
| OAuth 未配置 / `oauth_not_configured` | 管理员未配置 `MU_GITHUB_OAUTH_*` 三项——见 `.env.example` 注释 |
| `fixture_login_disabled` | fixture 操作员登录未开启——生产保持关闭，属预期安全行为 |
| `legacy_login_disabled_in_multiuser` | multiuser 形态禁用旧操作员密码登录——使用 GitHub OAuth |

### 登录与演示的默认关闭项（按部署模式）

| 开关 | 默认 | 说明 |
|---|---|---|
| `MU_ALLOW_FIXTURE_LOGIN` | 关 | fixture 操作员（`pilot-admin`）登录——仅开发/测试与首次建管使用，**不是正常生产登录方式** |
| `MU_LEGACY_LOGIN` | 关（multiuser 下强制关） | 旧操作员密码登录（`/api/auth/login`）——multiuser 形态无显式 `=1` 一律拒绝 |
| 演示入口 | multiuser 登录页不显示 | 未认证只读快照浏览，仅 legacy/本地形态保留 |

### 两种形态的配置示例（占位符，勿填真实值）

**本机 / 内网评估**（本机 OAuth 登录可用；无 webhook、无跨机器邀请）：

```bash
# .env（节选）
MU_MODE=multiuser
MU_GITHUB_OAUTH_CALLBACK_URL=http://127.0.0.1:48500/api/mu/auth/oauth/github/callback
# MU_ALLOW_FIXTURE_LOGIN=1        # 仅首次建管临时开启，建完即关
```

**公网 HTTPS 生产**（自动 webhook + 跨机器邀请认领）：

```bash
# .env（节选）
MU_GITHUB_OAUTH_CALLBACK_URL=https://<console 公开地址>/api/mu/auth/oauth/github/callback
# 反代：https://<console 公开地址> → 127.0.0.1:48500
#       https://<console 公开地址>/api/mu/github/webhook → 127.0.0.1:48590（GitHub App Webhook URL）
```

> 多租户、邀请与安装 GitHub App 的完整步骤见「多租户与 webhook」与 `docs/BETA-GUIDE.md`。

## 镜像获取与校验

| 途径 | 命令 | 校验 |
|---|---|---|
| GHCR | `docker pull ghcr.io/nghqqa/mergepilot-console@0077382db28d…` | RepoDigest == `sha256:0077382db28d454e7e03bbbd9ad3c2a1ddcd97cf84bc4cabe81c4d2b77610ad4` |
| 离线 tar | `docker load -i mergepilot-console-rc19.tar` | tar SHA256 见 `SHA256SUMS-rc19.txt`（发布附件） |
| 源码构建 | 见下节 | image ID 应可复现（同 commit/同构建参数） |

镜像内置 `MERGEPILOT_VERSION=0.2.0-beta.6-rc.19`（`/api/health` version 字段即真源）。

**供应链档案（rc.19 候选）**：

- SBOM：CycloneDX（`rc19-sbom.cdx.json`，随候选制品提供）；
- 漏洞/秘密扫描：trivy（`rc19-scan.json`，随候选制品提供）；
- 构建来源：候选 commit 见 `SHA256SUMS-rc19-candidate.txt` 同目录记录；**零 schema/迁移变更（恒 v23，与 rc.17 一致）**；
- 基础镜像：`node@sha256:0a7108bf…`（Dockerfile 钉死）。

> `48590` 端口与 `beta-webhook-proxy` 容器是 MergePilot 开发方内部测试环境的组件，
> **不属于自托管发行版，也不是官方托管服务入口**。自托管部署的对外入口是
> 你自己的 TLS 反向代理（见安全加固清单）。

## 从源码构建

```bash
git checkout v0.2.0-beta.6-rc.19        # 源码 tag（与镜像同源）
cd console/frontend && npm ci && npm run build && cd ../..
docker build -f docker/Dockerfile.canonical-console \
  --build-arg MERGEPILOT_VERSION=0.2.0-beta.6-rc.19 \
  -t ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.19 .
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
- cchain 引导（首次转 READY 需四步，缺一会 BLOCKED）：
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
  ④ provider attestation 静态端点：托管一个你自己生成的 `attestation.json`，把
     `MERGEPILOT_PROVIDER_ATTEST_URL` 指向 console 容器内可达的地址——完整内容、判定语义、
     三平台启动示例与重启恢复见下节「provider attestation」。
- RAG 四键：可选，不填则 RAG=NOT_WIRED 如实降级。

## provider attestation（cchain 第三键）

`MERGEPILOT_PROVIDER_ATTEST_URL` 指向一个**你自己托管的静态 JSON 端点**。它是 cchain
三组件之一（模型缓存 / provider attestation / keystore 运行绑定）：三组件全部就绪时
`/api/cchain/status` 报 `READY`；任一未就绪则整体 `BLOCKED`——**fail-closed，如实呈现，
不影响审查主链**，恢复后自动回到 READY。

### attestation.json 内容与来源

须为 JSON 且**至少含三个非空字段**（形状校验；无密码学验证——它证明的是部署者对本次
部署配置的自证，不是第三方签名）：

```json
{
  "provider": "my-mergepilot-node",
  "model": "bge-m3",
  "attestation": { "algo": "static-file-v1", "note": "本次部署的模型与配置说明" },
  "key_id": "optional-k1"
}
```

来源：自行生成（描述本次部署），放入静态服务目录即可。可选字段 `key_id`：设置了
`MERGEPILOT_PROVIDER_EXPECTED_KEY_ID` 时必须与其一致，否则判 INVALID。文件更新即时生效，
无需重启静态服务。

### 判定语义（与 console 运行时 `fetchProviderAttestation` 同口径）

| 情形 | 组件状态 → cchain |
|---|---|
| URL 未设置 | NOT_CONFIGURED → BLOCKED |
| 连接失败 / 超时（默认 5s，`MERGEPILOT_PROVIDER_TIMEOUT_MS` 可调） | UNREACHABLE → BLOCKED |
| HTTP 非 200（404 = 文件缺失或路径错） | UNREACHABLE → BLOCKED |
| 响应非 JSON / 缺 provider、model、attestation 任一字段 / attestation 为空 | INVALID → BLOCKED |
| HTTP 200 + 形状通过（+ 可选 key_id 一致） | ATTESTED → 计入 READY |

验证：`curl -fsS "$MERGEPILOT_PROVIDER_ATTEST_URL" >/dev/null && echo OK`；
或 `node preflight.mjs --live`（对 .env 里的 URL 做同口径探测，支持与本地源文件比对）；
最终以 console `/api/cchain/status` 为准。

### 启动示例（三平台）

监听一律绑回环或内网，**不要把 attest 端点暴露到公网**。

**方式一（推荐）：并入 docker compose（console 同网络直接可达，重启自动恢复）**

```yaml
# 追加到 docker-compose.yml 的 services: 下（ networks 用既有 internal ）
  attest:
    image: nginx:alpine
    restart: unless-stopped
    volumes:
      - ./attest:/usr/share/nginx/html:ro   # 目录内放 attestation.json
    networks: [internal]
```

`.env`：`MERGEPILOT_PROVIDER_ATTEST_URL=http://attest/attestation.json`
（服务名即内网主机名；主机/Docker 重启后随 `restart: unless-stopped` 自动恢复）。

**方式二：Linux systemd（宿主级静态服务）**

```ini
# /etc/systemd/system/mergepilot-attest.service
[Unit]
Description=MergePilot provider attestation (static)
After=network.target

[Service]
ExecStart=/usr/bin/python3 -m http.server 19475 --bind 127.0.0.1 --directory /opt/mergepilot/attest
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now mergepilot-attest`；`.env` 填
`http://host.docker.internal:19475/attestation.json`，并给 compose 的 **console** 服务加：

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

（Linux 宿主地址必须显式映射；Windows/macOS Docker Desktop 免配。`restart=always`
保证开机自启与进程死亡自动拉起。）

**方式三：Windows Task Scheduler（宿主级静态服务）**

管理员命令行注册开机启动：

```bat
schtasks /Create /TN "MergePilot-Attest" /SC ONSTART /RU SYSTEM ^
  /TR "cmd /c cd /d C:\mergepilot\attest && C:\path\to\python.exe -m http.server 19475 --bind 127.0.0.1"
```

`.env` 填 `http://host.docker.internal:19475/attestation.json`。
⚠️ 裸 `http.server` 无进程自愈：进程退出后须重新执行该命令；需要自动拉起请自备看护
脚本（属部署者本地运维选项，发行包不含）。

### 重启恢复路径与验证

| 托管方式 | 主机 / Docker 重启后 |
|---|---|
| compose attest 服务 | `restart: unless-stopped` 自动拉起；attestation.json 在 bind 目录，持久 |
| systemd | `Restart=always` + 开机自启，自动拉起 |
| Windows schtasks | ONSTART 开机启动；进程中途死亡无自愈，须手动重跑或自备看护 |

恢复后验证：`curl -fsS "$MERGEPILOT_PROVIDER_ATTEST_URL" >/dev/null && echo OK`，
再确认 `/api/cchain/status` 回到 `READY`。

## 多租户与 webhook

> **公网入口要求**：GitHub Webhook 必须有**公网可达的 HTTPS 入口**，并转发到本机 webhook
> 监听端口 `48590`（`webhook-ingress` 服务，仅放行 webhook 与 health 两个路径）。入口实现
> 不限：Nginx、Caddy、Cloudflare Tunnel 或其他任意反向代理/隧道均可，仓库不绑定特定
> ingress 实现。**没有任何公网入口时**，GitHub webhook 无法自动进入审查管线（本地管理
> 界面与健康检查不受影响）。

1. **GitHub App**：按最小权限创建（Metadata/Contents/Pull requests/Checks/Statuses 全只读；
   不申请任何 write）+ 订阅五种事件（installation、installation_repositories、pull_request、
   check_run、status）+ webhook URL 指向你的 ingress（默认 `http://<host>:48590/api/mu/github/webhook`，
   对外部署请换成 https 域名）；**务必配置与 `MU_GITHUB_WEBHOOK_SECRET` 一致的签名密钥**。
   - 登录入口：`https://<console 公开地址>/login`（GitHub OAuth；仅受邀成员可登录）。
     管理员在控制台「组织与接入 → 成员与邀请」面板创建邀请后可一键复制邀请链接：
     `https://<console 公开地址>/login?invite=<invite_id>`（origin 取 OAuth callback 的
     公开地址）。邀请绑定 GitHub 数字 id/租户/角色/有效期，单次认领，不可授予
     platform_admin——链接只是入口，不是可转让授权凭证。
2. **installation 注册**：安装 App 后跳转回控制台即注册到当前会话所属租户；
   租户成员的邀请认领（rc.14 起泛化、rc.16 持续）支持既有用户经邀请进入其他租户；
   同一 subject 存在多条待认领邀请时会显式拒绝（`invitation_ambiguous`）。
3. **隔离语义**：事件租户跟 installation 走；八张业务表按 tenant_id 强制收窄；
   撤权后同会话下一请求即 403；`platform_admin` 永不可经邀请授予（API/claim/DB 三层拒绝）。

## 首个平台管理员初始化（生产）

> 依据 rc.16 源码：`mu/store.mjs bootstrap()` 启动即创建 `default` 租户与 **fixture 引导操作员**
> （默认登录名 `pilot-admin`，provider=`fixture`，仅 `MU_ALLOW_FIXTURE_LOGIN=1` 时可登录——生产保持关闭）；
> OAuth 回调只接受**事先创建的邀请**（无邀请 → `not_invited`，无公共自动注册）；
> 邀请永不授予 `platform_admin`（API 校验 / claim 守卫 / DB CHECK 三层拒绝）。
> 因此**首个平台管理员由部署者在数据库直授**——这是设计行为，不是缺陷。

**前提**：console 已启动（`docker compose ps` 全部 healthy）、schema 已初始化至 v23；
已知自己的 GitHub 数字 user id（`https://api.github.com/users/<login>` 返回 JSON 的 `id` 字段）。

**步骤（DBA 执行，一次事务；`<login>`/`<id>` 换成你的值）：**

```bash
docker exec -it mergepilot-postgres-1 psql -U postgres -d mu
```

```sql
BEGIN;
-- 1) 建用户（login 仅展示用；身份键 = 下方 subject）
INSERT INTO mu.app_user (login, display_name)
VALUES ('<login>', '<显示名>')
ON CONFLICT (login) DO NOTHING;
-- 2) 绑定 GitHub 身份（provider/subject 必须与 OAuth 签发格式一致）
INSERT INTO mu.external_identity (user_id, provider, subject)
SELECT u.user_id, 'github-oauth', 'github-oauth:<id>'
FROM mu.app_user u WHERE u.login = '<login>'
ON CONFLICT (provider, subject) DO NOTHING;
-- 3) 硬校验：subject ↔ login 必须一一对应，且该 login 无其他 GitHub 身份——
--    不符即 RAISE（随后 ROLLBACK），不产生任何授权
SELECT u.login, i.subject FROM mu.app_user u
JOIN mu.external_identity i ON i.user_id = u.user_id
WHERE i.provider = 'github-oauth' AND i.subject = 'github-oauth:<id>';
DO $$
DECLARE matched int; other_ids int;
BEGIN
  SELECT count(*) INTO matched
    FROM mu.app_user u
    JOIN mu.external_identity i ON i.user_id = u.user_id
   WHERE i.provider = 'github-oauth' AND i.subject = 'github-oauth:<id>'
     AND u.login = '<login>';
  SELECT count(*) INTO other_ids
    FROM mu.external_identity x
   WHERE x.provider = 'github-oauth'
     AND x.subject <> 'github-oauth:<id>'
     AND x.user_id = (SELECT user_id FROM mu.app_user WHERE login = '<login>');
  IF matched <> 1 OR other_ids <> 0 THEN
    RAISE EXCEPTION '身份校验失败：subject↔login 匹配 % 行（须为 1），该 login 另有 % 个 GitHub 身份（须为 0）——请 ROLLBACK 并核对数字 id', matched, other_ids;
  END IF;
END $$;
-- 4) 直授 default 租户 platform_admin（UNIQUE(tenant_id,user_id) + NOT EXISTS，幂等）
INSERT INTO mu.membership (tenant_id, user_id, role)
SELECT t.tenant_id, i.user_id, 'platform_admin'
FROM mu.tenant t
JOIN mu.external_identity i ON i.provider = 'github-oauth' AND i.subject = 'github-oauth:<id>'
WHERE t.slug = 'default'
  AND NOT EXISTS (SELECT 1 FROM mu.membership m
                  WHERE m.tenant_id = t.tenant_id AND m.user_id = i.user_id);
COMMIT;
```

**验证**：

1. 库内：`SELECT u.login, m.role, m.state FROM mu.membership m JOIN mu.app_user u ON u.user_id = m.user_id WHERE m.role = 'platform_admin';` —— 应看到你的 login 且 `state=active`；
2. 登录：浏览器 GitHub OAuth 登录后，右上角应显示「组织 default · 平台管理员」；`GET /api/mu/session` 的 `role` 为 `platform_admin`。

**执行方式与行为说明**（以下行为均经隔离环境整块验收，非推测）：

- 脚本化/自动化执行：把上方 SQL 块原样存为 `init.sql` 后交由 psql 执行，并**同时检查退出码与提交后的数据状态**：

  ```bash
  docker cp init.sql mergepilot-postgres-1:/tmp/init.sql
  docker exec mergepilot-postgres-1 psql -U postgres -d mu -v ON_ERROR_STOP=1 -f /tmp/init.sql
  ```

  成功判据 = 退出码 0 **且**上方「验证」查询能看到你的 login（`state=active`）。
- **未启用 `ON_ERROR_STOP` 时，SQL 中途出错 psql 仍可能返回退出码 0**（此时事务已被服务端整体回滚，库内不存在任何部分授权）。退出码 0 不得单独作为初始化成功的依据——必须以「验证」查询的数据状态为准。
- 不建议用 shell 双引号把含 `$$` 的整段 SQL 传给 `psql -c`：双引号内 `$$` 会被 shell 展开为当前 shell 的 PID，产生 `DO <pid> …` 之类语法错误。推荐 SQL 文件（`-f`）或交互式粘贴输入，避免该展开。
- 初始化块可安全重跑：步骤 1/2/4 对已存在行均为 no-op（`ON CONFLICT DO NOTHING` / `NOT EXISTS`），**不覆盖任何已有数据**——包括此前手动做出的降级：角色改为 `maintainer` 后重跑本块，角色仍保持 `maintainer`，这是预期行为（防止已降权账号被旧脚本重新升权）。

**回退 / 收权**（后续成员一律走邀请，见「多租户与 webhook」与 BETA-GUIDE §6.3，勿再直授）：

```sql
-- 角色给错：降级为最小必要角色
UPDATE mu.membership SET role = 'maintainer'
WHERE tenant_id = (SELECT tenant_id FROM mu.tenant WHERE slug = 'default')
  AND user_id = (SELECT user_id FROM mu.app_user WHERE login = '<login>');
-- 彻底移除成员（可逆：重新直授或改走邀请）
DELETE FROM mu.membership
WHERE tenant_id = (SELECT tenant_id FROM mu.tenant WHERE slug = 'default')
  AND user_id = (SELECT user_id FROM mu.app_user WHERE login = '<login>');
```

收权语义与 `platform_admin` 恢复边界：

- 收权（`UPDATE` 降级 / `DELETE` 移除）**只作用于 `mu.membership` 行**，不删除 `mu.app_user` 用户记录与 `mu.external_identity` 身份绑定——账号本体保留，后续可改走邀请加入其他角色。
- 邀请**永不能授予或恢复** `platform_admin`（API 校验 / claim 守卫 / DB CHECK 三层拒绝）。恢复平台管理员属于**部署者明确执行的 DBA 授权操作**（直接调整对应 membership 行的 role，或按需移除该行后重新直授）——不把「删除 membership 后重跑初始化块」作为默认建议路径。

**注意**：直接 SQL 不产生应用审计事件（DBA 面操作），请在你的变更记录中自行留痕。

## 升级与回滚

```bash
# 升级：改 compose 里 console 镜像引用（digest 或版本 tag）后
docker compose --env-file .env pull console && docker compose --env-file .env up -d
# 回滚：把引用改回旧 digest 再 up -d。数据卷不动。
```

**schema 只前进不降级**：新版可能自动前移 `mu.schema_migrations`；回滚镜像后旧代码
对新列/新表无感知（历史兼容已验证），但**不可跨过 schema 破坏性操作**——升级前
`pg_dump` 一份（见下）。全部历史迁移为 additive（rc.5→rc.16 实证）。

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
| cchain=BLOCKED | 三键是否齐全；attestation 端点可达性（`node preflight.mjs --live` 可探测，含内容一致性比对）；keystore 是否已 rotate 出在役 key |
| 登录后 403 membership_inactive | 会话绑定租户的成员关系已被撤——重新登录会落到其余 active 租户 |

## 已知限制

- 无租户切换端点/UI（多租户用户以对应租户邀请重新登录）；
- 私有 GitHub App 仅所有者账号可安装；多租户 webhook 需公开化 App 或每租户独立 App；
- webhook ingress 仅放行 webhook 与 health 两个路径（管理面经 48500 直连或反代）。
