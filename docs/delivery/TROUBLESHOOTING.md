# MergePilot 交付排障手册（rc.35.4 口径）

> 按症状组织。每条：现象 → 原因 → 具体处理步骤。
> 先跑只读诊断工具定位失败面（退出码 0=通过 / 1=有失败项）：
>
> ```bash
> node scripts/delivery/diagnose.mjs --env deploy/selfhost/.env \
>   --base-url http://127.0.0.1:48500 --pg-container mergepilot-postgres-1
> ```

## T-ENV — 环境变量与 .env 问题

**现象**：诊断工具 `env_*` 项 FAIL；或 console 启动即退出，日志报 compose required 变量缺失。

处理：
1. `cp .env.example .env` 后逐段核对必填键（数据库/会话、GitHub App 五项、OAuth 三项、LLM 四项）。
2. secret 生成：`openssl rand -hex 32`（webhook secret）、`openssl rand -base64 32`（会话密钥）。
3. `.env` 改动必须 `docker compose --env-file .env up -d` 重启才进容器——改完不重启等于没改。
4. 诊断工具显示「已设置(N 字符)」即表示该键已进入环境；键值本身任何工具都不会回显。

## T-DB — 数据库 / schema 未就绪

**现象**：`/api/health` 里 `mu_schema_ready:false`；诊断 `http_schema_ready` 或 `db_schema` FAIL；
日志报迁移错误。

处理：
1. `docker compose ps` 确认 postgres healthy；`docker compose logs postgres` 看初始化错误。
2. 确认 pgdata 卷未被旧实例占用（换过 `name:`/卷名会出现"两套数据"）。
3. schema 由 console 启动时**自动迁移**（幂等、只前进，当前至 v28）。手动核对版本：
   ```bash
   docker exec mergepilot-postgres-1 psql -U postgres -d mu -tAc \
     "SELECT COALESCE(MAX(version),0) FROM mu.schema_migrations"   # 期望 >= 28
   ```
4. 不要手工改 mu schema；升级只换镜像 digest 后重启。schema 未就绪时审查接口如实拒绝（不降级带病服务）。

## T-OAUTH — 登录 / OAuth 回调问题

**现象**：登录页无 GitHub/Gitee 入口；登录跳转后报 `oauth_not_configured`；受邀人登录报 `not_invited`。

处理：
1. 诊断 `http_providers` 显示的是**服务端视角**的配置态——若 FAIL，核对 `.env` 三项是否真实进入容器（见 T-ENV 第 3 条）。
2. 回调地址必须与实际访问地址**同源**：`<console 公开地址>/api/mu/auth/oauth/github/callback`。
   回环地址仅本机可用（诊断会 WARN）；跨机器/公网用 HTTPS 域名。
3. `not_invited` / 邀请类错误（`invitation_not_found` / `invitation_expired` / `invitation_already_claimed`
   / `invitation_ambiguous`）：邀请按**平台数字 user id** 绑定（GitHub 用 `https://api.github.com/users/<login>` 的 `id`），
   单次认领、默认 24h 有效——请管理员重建邀请。
4. `fixture_login_disabled` / `legacy_login_disabled_in_multiuser`：预期安全行为，生产走 OAuth。
5. `MU_ALLOW_FIXTURE_LOGIN=1` 仅用于首次建管/开发，建完即关（诊断会 WARN）。

## T-WEBHOOK — webhook 未到达 / 事件不进管线

**现象**：PR 更新后无新 run；GitHub App 高级页 Recent Deliveries 为空或非 2xx。

处理：
1. GitHub App 设置的 Webhook URL 必须**公网可达**（回环地址 GitHub 服务器访问不到），
   路径 `/api/mu/github/webhook`，经反代/隧道转到本机 `WEBHOOK_INGRESS_PORT`（默认 48590）。
2. `MU_GITHUB_WEBHOOK_SECRET` 与 App 设置页的 secret 逐字一致（验签失败即拒收）。
3. 事件订阅含 `pull_request`；App 安装且 binding `active`（仓库页确认）。
4. 消费循环：默认开启（45s 节拍）。若设置过 `MU_JOB_CONSUMER_ENABLED=0`，事件滞留队列，
   需人工 `POST /api/mu/jobs/tick`——诊断 `job_consumer` FAIL 即此因，移除该设置并重启。
5. 验证端到端：App 高级页 **Redeliver** 一条历史投递 → 约 1 分钟内出现新 run。
6. Gitee：webhook 走 Gitee 仓库设置里的钩子地址 `/api/mu/gitee/webhook`（密码模式，见部署配置）。

## T-BINDING — 仓库绑定问题

**现象**：「安装 / 更新 GitHub App」报 `github_app_not_configured` / `github_app_slug_not_configured`；
绑定后仓库页无仓库列表。

处理：
1. GitHub App 五项 env 齐全（诊断 `env_github_app`）；`MU_GITHUB_APP_SLUG` 是
   `github.com/settings/apps/{slug}` 的 `{slug}`，不是显示名。
2. 安装回调 `MU_GITHUB_APP_INSTALL_CALLBACK_URL` 指向 `<公开地址>/multiuser`。
3. 绑定动作需 maintainer 或 platform_admin（`manage_repository_binding`）。
4. 一个 GitHub 仓库全局只能被一个租户绑定（唯一约束）；换租户先解绑。
5. App 被卸载 → installation 与全部 binding 自动 `revoked`（fail-closed），重装后重新绑定。

## T-EXECUTOR — 执行器 / 修复轮不可用

**现象**：审批后修复轮不启动；attempt 错误码 `EXECUTOR_MODE_UNSET` / `EXECUTOR_MODE_INVALID`
/ `EXECUTOR_INTERNAL_NOT_ALLOWED` / `MT_NOT_CONFIGURED`。

处理：
1. 未配置 `MU_EXECUTOR` → 修复面 fail-closed（属预期安全行为）。需要修复能力时按
   [DEPLOY-GUIDE 目标三](DEPLOY-GUIDE.md#目标三启用修复验证审批门--补丁--可应用性--修复测试)配齐
   `MU_EXECUTOR=agentteams` 七项。
2. Matrix 三项（URL/USER/PASSWORD）缺失 → 任务传输拒绝（`MT_NOT_CONFIGURED`）。
3. `MU_EXECUTOR=internal` 仅限开发/测试/应急，且必须同时设 `MU_EXECUTOR_INTERNAL_ALLOW`。
4. 执行器健康：控制台「组织与接入」的执行器面板显示当前模式与状态。

## T-MODEL — LLM 第二阶段未执行 / 模型授权问题

**现象**：run 详情显示模型阶段失败；错误码 `LLM_POLICY_ENV_MISMATCH` /
`LLM_POLICY_MODEL_NOT_ALLOWED` / `EGRESS_DENIED:<reason>`。

处理（按错误码）：
1. `LLM_POLICY_ENV_MISMATCH`：`.env` 的 `MU_LLM_PROVIDER/BASE_URL/MODEL/API_KEY` 四项缺失或
   provider 不是 `openai_compatible`——补齐并重启（诊断 `env_llm`）。
2. `LLM_POLICY_MODEL_NOT_ALLOWED`：运行策略里的模型不在 `MU_LLM_ALLOWED_MODELS` 白名单——
   把模型加入白名单或改用白名单内模型（默认 `deepseek-flash,deepseek-chat`）。
3. `EGRESS_DENIED:EGRESS_POLICY_MISSING` / `EGRESS_MODE_NOT_EXTERNAL`：租户出站策略未设为
   `external_api` 或未提交同意记录——platform_admin 在设置面完成「策略 + 同意」两步。
4. `EGRESS_DENIED:EGRESS_CONSENT_REVOKED`：同意被撤销——重新提交同意。
5. `EGRESS_DENIED:EGRESS_PROVIDER_BLOCKED`：provider 策略态为 blocked——改回 verified/custom_acknowledged。
6. 语义提醒：LLM 阶段失败**不影响**静态规则结果照常可用；系统绝不把"模型未执行"伪装成"已执行"。

## T-FIXTEST — 实际修复测试未执行 / 命令不生效

**现象**：验证三分中「实际修复测试」恒为未执行；或配置了命令但未按预期执行。

处理：
1. 未设置 `MU_FIX_TEST_CMDS` → 如实「未执行」。按仓库配置：
   `MU_FIX_TEST_CMDS={"gitee:owner/repo":"npm test"}`（key 兼容 `owner/repo`；改后重启）。
2. 诊断 `fix_test_cmds` FAIL = JSON 不合法——坏 JSON 等同未配置（不猜命令、不跑模型输出里的指令）。
3. 测试命令**只**来自该部署配置；`MU_FIX_TEST_ALLOW_DEPS_CMD=1` 仅供隔离 E2E，生产必须移除（诊断 WARN）。
4. 「修复前即通过」（`pre_test_passed`）= 测试没覆盖该缺陷，结果如实为未通过——换能复现缺陷的命令。

## T-GITEE — Gitee 接入问题

**现象**：Gitee 登录入口不渲染；Gitee 仓库连接失败；Gitee 补丁可应用性显示「未验证」。

处理：
1. 登录入口 fail-closed：`MU_GITEE_OAUTH_*` 三项任一缺失即不渲染（预期）。配置见
   [BETA-GUIDE「Gitee OAuth 登录」](../BETA-GUIDE.md#gitee-oauth-登录可选wave-gitee)。
2. 仓库连接 / 评论发布 / 可应用性检查的私有仓库读取依赖部署级 `MU_GITEE_PAT`——缺失时相关项
   如实降级（`gitee_credentials_missing`）。
3. Gitee webhook：仓库设置里的 hook 指向 `<公开地址>/api/mu/gitee/webhook`（密码模式）。

## T-PUBLISH — 发布评论失败

**现象**：发布报 409 `head_moved` / `not_verifiable`；502 `remote_unreachable`；评论没出现。

处理：
1. `head_moved`：远端 head 已推进，陈旧结论拒绝发布——重新审查后用新 run 发布（预期防护，不是故障）。
2. `not_verifiable`：run 状态未到可发布态（仅 `COMPLETED`/`VERIFIED` 可发布）——等审查/修复链完成。
3. `remote_unreachable` / `publish_failed`：检查机器人凭据——GitHub 走 App installation token
   （App 需已安装且 binding active）；Gitee 走 `MU_GITEE_PAT`（需该仓库评论权限）。
4. 状态 `unconfirmed`（超时/结果不明）：先在 PR 页核实评论是否已写出，**不要盲目重发**；
   同 run 重发幂等（原位更新）。
5. 同 run 重复发布只会更新机器人自己的既有评论（marker 定位），不会覆盖人工评论。

## T-BOT — 机器人身份与权限

**现象**：评论作者不是预期机器人；GitHub 侧 403/404。

处理：
1. 写入身份固定为 **MergePilot GitHub App**（installation token）或 **MergePilot Gitee 机器人**
   （部署 PAT）——发布预览页会明确显示，不会用个人账号发。
2. GitHub App 需安装在目标仓库所在账号/组织；installation 被吊销/暂停后一切远端动作 fail-closed。
3. 本版本机器人的**唯一**远端写动作是发布 PR 评论；不存在提交代码/合并路径（开发中能力见
   [CAPABILITY-MATRIX](CAPABILITY-MATRIX.md)）。

## T-HTTP — 服务不可达 / health 异常

**现象**：`curl /api/health` 失败；诊断 `http_health` FAIL。

处理：
1. `docker compose ps` 看 console 是否 healthy；`docker compose logs console` 看启动错误。
2. 端口：宿主侧默认 `127.0.0.1:48500`（容器内 4730）；`CONSOLE_PORT` 可改。
3. `/api/health` 的 `version`/`git_commit` 用于核对所跑镜像与发行说明一致。
4. 对外暴露必须前置 TLS 反代；明文 HTTP 不允许承载已认证会话。

## T-CCHAIN — cchain=BLOCKED（可选组件）

**现象**：签名验证页显示 BLOCKED。

处理：三缺口按 [DEPLOY-GUIDE「可选：RAG / cchain」](DEPLOY-GUIDE.md#可选rag--cchain信任链)引导：
模型卷 manifest → `fxv.audit_events` 建表 SQL → keystore 种子密钥（uid 1000，目录 770/文件 600）
→ provider attestation 端点。三键不全时 BLOCKED 属**如实呈现**，不影响审查主链。
