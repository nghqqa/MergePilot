# MergePilot rc.17 自托管部署

> 版本：`0.2.0-beta.6-rc.17` · 镜像 digest：`sha256:112284f104001ac5c94b7d6c64ed2b6c854b52ec1c96dee275268bd7ee0e380a`
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
| GHCR | `docker pull ghcr.io/nghqqa/mergepilot-console@sha256:112284f104…e380a` | RepoDigest == 该 digest |
| 离线 tar | `docker load -i mergepilot-console-rc17.tar` | tar SHA256 = `2a8ccce4b7fe607c532665581a50bf8e5a021f2ace2a75fbd87c63b913030b90`；load 后 image ID = `112284f104…e380a`（本版 config ID 与 GHCR manifest digest 观测同值） |
| 源码构建 | 见下节 | image ID 应可复现（同 commit/同构建参数） |

镜像内置 `MERGEPILOT_VERSION=0.2.0-beta.6-rc.17`（`/api/health` version 字段即真源）。

**供应链档案（rc.17）**：

- SBOM：CycloneDX 1.7，**35 组件**（`rc17-sbom.cdx.json`，随发行版发布）；
- 漏洞/秘密扫描（trivy 0.74，ghcr.io 官方漏洞库）：**vulnerabilities=0、secrets=0**；
- 构建来源：源码 tag `v0.2.0-beta.6-rc.17` = main commit `31491a3`（与镜像 digest `112284f104…e380a` 同源）；
  相对 rc.16 运行时差异：登录/邀请 UX（前端 6 文件）+ session 能力信号（session.mjs）+ start?invite= 非法格式 404（api.mjs）；**零 schema/迁移变更（恒 v23）**；
- 基础镜像：`node@sha256:0a7108bf…`（Dockerfile 钉死）。

> `48590` 端口与 `beta-webhook-proxy` 容器是 MergePilot 开发方内部测试环境的组件，
> **不属于自托管发行版，也不是官方托管服务入口**。自托管部署的对外入口是
> 你自己的 TLS 反向代理（见安全加固清单）。

## 从源码构建

```bash
git checkout v0.2.0-beta.6-rc.17        # 源码 tag（与镜像同源）
cd console/frontend && npm ci && npm run build && cd ../..
docker build -f docker/Dockerfile.canonical-console \
  --build-arg MERGEPILOT_VERSION=0.2.0-beta.6-rc.17 \
  -t ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.17 .
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

## 已知限制（rc.17）

- 无租户切换端点/UI（多租户用户以对应租户邀请重新登录）；
- 私有 GitHub App 仅所有者账号可安装；多租户 webhook 需公开化 App 或每租户独立 App；
- webhook ingress 仅放行 webhook 与 health 两个路径（管理面经 48500 直连或反代）。
