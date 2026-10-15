# rc.35.5 部署模板隔离验证与首次使用走查（文档线，代理自检）

> 执行：PR #468 文档线（代理自检——无真实新用户参与，**不构成"用户验收通过"**）。
> 日期：2026-10-15 ｜ 基线：main@1fcb47c + 文档线改动（`docs/rc355-firstuse`）。
> 边界：独立 compose 项目名/数据库/卷/网络/端口（48610/48611）；**零真实凭据**（dummy 值 +
> 一次性生成的假 App 私钥）；未触碰生产队列、执行器、共享候选容器与任何生产配置；
> 验证后隔离资源已全部清理（见 §8）。

## 1. Compose 模板解析（含守卫行为）

| 用例 | 命令要点 | 结果 |
|---|---|---|
| A. 缺必填守卫 | 去掉 `POSTGRES_PASSWORD` 后 `docker compose --env-file <f> config` | ✅ 拒绝：`required variable POSTGRES_PASSWORD is missing a value: 请在 .env 设置 POSTGRES_PASSWORD`，退出码 1 |
| B. dummy 全量解析 | 按 `deploy/selfhost/docker-compose.yml` 文档流（模板 `env_file: .env` 要求同目录存在 `.env`；`--env-file .env` 同源注入） | ✅ 解析成功退出码 0；console 镜像=pinned `@sha256:e304…3a`；`CONSOLE_PG_DSN` 指向 `postgres:5432`；`-p` 覆盖项目名为 `mpdocsverify` 生效 |
| C. 多行 PEM 注入 | `.env` 内 `MU_GITHUB_APP_PRIVATE_KEY="<PEM \n 转义单行>"` | ✅ 解析后值为真实换行（compose 对双引号值处理转义），服务可启动。注：私钥正确性只在调用 GitHub API 时校验，boot 不校验——本项仅验证模板注入语义 |

## 2. 隔离栈启动与 schema

- `docker compose -p mpdocsverify up -d`：console 约 **5 秒 healthy**；postgres healthy；webhook-ingress 启动并对 `/api/health` 回 200（48611）。
- `GET /api/health`（48610）：`ok:true`，`version: v0.2.0-beta.6-rc.35.5`，`git_commit: af8b0022`，`mu_schema_ready: true`——与生产冻结身份一致。
- **新库自动迁移至 v29**：隔离 pg 内 `SELECT MAX(version) FROM mu.schema_migrations` = **29**；`mu.fix_apply_operation` 表存在。

## 3. 只读诊断工具（零依赖单文件直跑）

`node scripts/delivery/diagnose.mjs --env .env --base-url http://127.0.0.1:48610 --pg-container mpdocsverify-postgres-1`
→ **14 项全 PASS，退出码 0**（含 env/密钥、GitHub App 五项、OAuth 三项、LLM 四项+白名单、`MU_FIX_TEST_CMDS`、job 消费循环、health、schema、服务端登录配置、隔离库 schema 版本）。

## 4. 首个平台管理员初始化（按 selfhost README 的 SQL 块原样执行）

- 以文档 SQL 块替换 `<login>/<显示名>/<id>` 为 dummy 值，`docker cp` + `psql -v ON_ERROR_STOP=1 -f` 执行：**退出码 0，COMMIT**；
- 验证查询返回 `docsadmin | platform_admin | active` ✅；
- **幂等重跑**：再次整块执行 → `INSERT 0 0`（no-op），记录数仍为 1 ✅（与文档"可安全重跑、不覆盖已降权角色"一致）。
- 执行注意（Windows Git Bash）：`docker exec … psql -f /tmp/init.sql` 的 `/tmp` 参数会被 MSYS 路径转换破坏——需 `MSYS_NO_PATHCONV=1`（Linux/macOS 不受影响；文档无需改动，属本机 shell 特性）。

## 5. 首次使用走查（隔离实例，代理自检）

走查依据 = [USER-GUIDE.md](../../delivery/USER-GUIDE.md) 路径 B（§4 邀请成员）与路径 A 的服务端前置：

| # | 步骤（按文档） | 预期 | 实际 | 结论 |
|---|---|---|---|---|
| W1 | bootstrap 引导操作员存在（隔离新库） | default 租户 + `pilot-admin`（fixture，生产关闭登录） | 存在，role=platform_admin，state=active | ✅ |
| W2 | fixture 登录 `POST /api/mu/auth/login {provider:fixture,subject:fixture:pilot-admin}`（隔离实例设 `MU_ALLOW_FIXTURE_LOGIN=1`；生产默认关闭——关闭态由 diagnose/代码保证，本轮未在生产复测） | 会话+CSRF | ✅ `ok:true, role:platform_admin, tenant:default` | ✅ |
| W3 | `GET /api/mu/session` 动作投影 | 与 authz.mjs 一致 | 返回数组=**`read_repository, read_pull_request, rag_query, request_review, request_repair, manage_repository_binding, manage_membership, read_audit, manage_instance`**——确无 `read_code_content`/`decide_review`/`apply_fix`/`merge_pr` | ✅（本文档权限矩阵拆行的实测依据） |
| W4 | 无 CSRF 创建邀请 | 403 | ✅ 403 `csrf_required` | ✅ |
| W5 | 带 CSRF `POST /api/mu/invitations {expected_subject:"github-oauth:<数字id>", role:reviewer}` | 创建成功 | ✅ 邀请创建（id b9772f78…，role=reviewer） | ✅ |
| W6 | 受邀人 OAuth 认领、GitHub App 安装/绑定、webhook、LLM 出站、执行器、真实修复链 | — | **未验证**（无真实 GitHub/Gitee 凭据与公网入口；不伪造） | ⏳ 标注未验证 |

生产实例只读核验（无会话，代理自检）：`/api/health` 身份四元组与冻结一致；登录页可达。
A 线（受邀用户主链）真实操作记录以独立验收线为准：[FIRST-USE-ACCEPTANCE.md](FIRST-USE-ACCEPTANCE.md) §1
（维护者本人逐步操作 + evidence/01~07 截图）——本线不重复、不覆盖其结论。

## 6. 镜像三对象核验（分列，实测）

| 对象 | 值 | 核验方式（实测通过） |
|---|---|---|
| OCI index digest | `sha256:e30419990434c209de8d9de7fc11fc267d8c9e6fe0d99ff242eda69b7a61dd3a` | `docker buildx imagetools inspect <tag>` Digest 行 = 该值；与 tag `v0.2.0-beta.6-rc.35.5` 同源 |
| linux/amd64 平台 manifest | `sha256:efba4a6c…26fe` | index raw JSON `manifests[]`（另一条 `unknown/unknown` 为 attestation，正常） |
| amd64 config digest | `sha256:65e7d2a4…06a` | 平台 manifest raw JSON `config.digest` |
| 本地 image ID（`.Id`） | 本机实测 = index digest | **引擎相关**：Docker Engine 29.7.2（containerd 存储）下 `.Id`=index digest；经典存储引擎下 `.Id`=config digest。**跨对象不要求相等；跨引擎不比较 `.Id`** |

- **在线拉取**：`docker pull …@sha256:e304…` 后 `RepoDigests` 含 `@sha256:e304…`（与引擎无关的权威核验）✅。
- **离线加载**：Release 附件 tar 实测 `sha256sum` = `692727cc…793c`（与 `SHA256SUMS` 一致）→ `docker load` → `.Id` 与在线拉取一致 ✅。
- **生产一致性**：`beta-mp-console` 容器 `.Image` = `sha256:e304…`，`/api/health` 身份同值 ✅。
- 修正说明：下载页此前把同一值同时当作 index digest 与 `docker inspect .Id` 期望值——已分列更正（`docker inspect .Id` 不再给统一期望值）。

## 7. 未验证项（如实标注）

外部 OAuth 登录/认领、GitHub App 安装与 webhook、LLM 真实出站、AgentTeams 执行器、
`MU_FIX_TEST_CMDS` 真实执行、修复测试 pre/post、生产 UI 页面（无浏览器会话）——
均无测试凭据/入口，不伪造结果；相关文档步骤维持"按文档执行+未验证"状态。

## 8. 清理记录

`docker compose -p mpdocsverify down -v` 后复查：无 mpdocsverify 容器/卷/网络残留；
测试 `.env`/cookie/SQL 已删除；隔离验证目录仅存模板副本与一次性假密钥（不入库）。
生产与共享候选容器未做任何变更；离线 tar 载入镜像与生产同 ID（等同已存在镜像，未删除）。
