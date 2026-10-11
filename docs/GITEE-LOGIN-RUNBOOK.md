# Gitee 登录上线 Runbook（Wave-Gitee / PR #402）

> 受众：执行 Gitee OAuth 登录上线的运维/维护者。本文是 #402 合入后的唯一上线操作手册。
> 边界：上线窗口与执行器（AgentTeams/QwenPaw）替换**分开安排**；本文不含执行器内容。

## 0. 前置输入（不可替代清单——缺任一即停在准备态）

| # | 输入 | 来源 | 用途 |
| --- | --- | --- | --- |
| 1 | `MU_GITEE_OAUTH_CLIENT_ID` | gitee.com → 设置 → 第三方应用 → 创建应用（需持有 Gitee 账号者在网页操作） | OAuth 应用标识 |
| 2 | `MU_GITEE_OAUTH_CLIENT_SECRET` | 同上（创建后一次性展示） | token 交换 |
| 3 | `MU_GITEE_OAUTH_CALLBACK_URL` | `<Console 公开地址>/api/mu/auth/oauth/gitee/callback`（须与 Gitee 应用后台登记的"授权回调地址"逐字符一致，且浏览器可达） | 授权回跳 |

2026-10-11 核查：现有秘密源（secrets/、prod-data/secrets/）仅有 forge 仓库接入凭据（gitee-pilot.env：PAT/webhook secret/repo），**无 OAuth 登录应用凭据**——三项均需运营者在 Gitee 后台新建应用后提供。凭据注入方式与 GitHub OAuth 三项一致（Console 容器 env），**不落库不入审计**。

## 1. 部署步骤（幂等，可在维护窗口重跑）

1. 前置确认：生产 Console ≥ 含 #402 的首个发布版（版本号由发布轮指定，本文不预设）。
2. 注入三项 env（与现有 `MU_GITHUB_OAUTH_*` 相同注入面）。
3. 滚动重建 Console 容器——启动时 schema 懒迁移 v25→v26（`mu_oauth_flow` 增 `provider`/`bind_user_id` 两列，additive 幂等；存量行自动回填 `provider='github-oauth'`，ghapp_install 安装流语义不变）。
4. 迁移验证：`SELECT version FROM mu.schema_migrations WHERE version=26` 恰一行；`SELECT provider, count(*) FROM mu.oauth_flow GROUP BY provider` 存量行全为 `github-oauth`。
5. 冒烟：`GET /api/mu/auth/providers` 返回 `gitee.configured:true`；登录页出现「使用 Gitee 登录」次入口。

## 2. 真实验收步骤（用户本人操作账号授权；不以 mock 冒充）

按 A/B/C 三场景执行并留证（截图/审计查询），任何一步失败即停止上线。
场景边界（2026-10-12 澄清）：**已绑定到某账号的 Gitee 身份不能被另一账号"转移"或
"合并"**——绑定冲突即拒绝（C 场景）。B 场景要求目标 Gitee 身份处于未被认领状态。

**A. 独立 Gitee 邀请入驻及再次登录**（受邀 Gitee 身份独立入驻）

1. 邀请管理面板选 Gitee 平台、填受邀者 Gitee 数字 user id（`gitee-oauth:<id>`）→ 受邀者打开 `/login?invite=<id>` → 从「使用 Gitee 登录」进入 Gitee 授权 → 落地 /multiuser，角色=邀请角色；审计 `MU_LOGIN(provider=gitee-oauth)` + flow `invite_bound:true`。
2. 再次登录：退出后直接 Gitee 登录 → 同一用户（`external_identity` 单行），无重复建户。

**B. GitHub 用户绑定未被认领的 Gitee 身份及绑定后登录**

3. 已有 GitHub 会话 → 设置页「账号绑定」→「绑定 Gitee 账号」（目标 Gitee 身份未被任何账号绑定）→ Gitee 授权 → 回设置页显示两条身份（`MU_IDENTITY_BOUND`）；成员关系/角色不变。
4. 绑定后登录：退出，用 Gitee 入口登录 → 进入同一账号（与 GitHub 会话相同 user_id）。
5. 双来源标签：同一账号分别经 GitHub / Gitee 登录，设置页身份行与顶栏能力条随**本次认证来源**变化（`login_type` 两 session 端点一致，契约见 `lib/muLoginType.mjs`）。

**C. 已占用身份绑定冲突拒绝**

6. 对已绑定到账号 X 的 Gitee 身份，账号 Y 发起绑定 → 回跳 `mu_bind_error=identity_already_bound`（文案「该 Gitee 身份已绑定到某个账号……不能重复绑定」），X 的身份与成员关系不受影响（不抢绑）。

**角色保持**（贯穿 A/B）：全程 `/api/mu/session` 的 role 与登录前一致；活跃成员认领低角色邀请 → 角色保持 + `MU_INVITATION_ACKNOWLEDGED_NO_CHANGE`（#401 语义）。

## 3. 降级边界（2026-10-11 隔离演练实证，10/10+10/10）

- **升 v26 后回滚到 rc.24 应用**：GitHub 登录/邀请/安装流/权限投影**全部原样工作**（v26 两列对旧代码透明）；Gitee 端点在 rc.24 上被拒（无会话 401/有会话 404，fail-closed）；`providers` 响应无 `gitee` 键；新版写入的 gitee 行可被旧代码通用 CAS 消费（无阻塞）。
- **语义边界**：回滚后 Gitee 身份**不能登录**（路由不存在），但已绑定身份的数据保留（可再升级恢复）；Gitee 会话签发的会话在 rc.24 前端显示"登录来源未知"（旧 identity.js 不识别 gitee-oauth——诚实呈现，不冒充）。
- **降级操作**：Console 镜像回滚到 rc.24 tag 即可；**不需要**回滚 schema（v26 无破坏性）。

## 4. 回滚步骤

1. Console 镜像 tag 回滚（rc.24 或上一个含 #402 前的版本）。
2. 观察登录（GitHub 主链）+ `providers` 响应恢复旧形状。
3. （可选）如需彻底移除 gitee 数据：`ALTER TABLE mu.oauth_flow DROP COLUMN IF EXISTS provider; ALTER TABLE mu.oauth_flow DROP COLUMN IF EXISTS bind_user_id; DELETE FROM mu.schema_migrations WHERE version=26;`——仅在确认不再升级时执行（正常回滚不需要）。

## 5. 上线后观察点（首 24h）

- `mu.platform_audit_event` 中 `OAUTH_FLOW_REJECTED` 的 reason 分布（新增 `invite_provider_mismatch` 只应出现在错入口场景）。
- `mu.session.provider='gitee-oauth'` 会话数与实际使用者核对。
- 登录页双入口渲染（providers 驱动）在移动端/桌面端各一次人工过目。

## 6. 责任边界

- Gitee 应用创建/凭据提供：运营（Gitee 账号持有者）。
- env 注入与滚动：运维（本文 §1）。
- 真实验收执行：§2 五步由维护者本人浏览器操作（不代批、不 mock）。
