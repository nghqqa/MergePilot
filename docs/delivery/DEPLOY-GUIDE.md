# MergePilot 部署指南（按目标分步，rc.35.6-rc2 交付口径）

> 本文按**你要达成什么**组织，每步末尾给验证命令。能力边界见
> [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md)；失败处理见
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
> 拓扑与镜像 digest 的完整说明见 [`deploy/selfhost/README.md`](../../deploy/selfhost/README.md)（本文是其按目标精简的交付入口）。

## 目标一：最小部署——先能完成审查

### 0. 前提

- Docker Engine 24+ / Docker Compose v2；2GB 可用内存（不开 RAG）。
- 一个用于对外访问的地址：本机评估用 `127.0.0.1`；要收 GitHub webhook 则必须有
  **公网 HTTPS 入口**（反代/隧道指向本机端口，实现不限）。
- 工具文件：`deploy/selfhost/docker-compose.yml`、`.env.example`、`webhook-proxy.js`。

### 1. 准备 .env

```bash
cd deploy/selfhost
cp .env.example .env          # 权限建议 chmod 600
```

**必填段**（生成命令见 `.env.example` 注释）：

| 段 | 键 | 说明 |
|---|---|---|
| 数据库/会话 | `POSTGRES_PASSWORD`、`CONSOLE_SESSION_SECRET` | compose 用密码拼 DSN，密码不出现在第二处 |
| GitHub App | `MU_GITHUB_APP_ID` `_APP_SLUG` `_PRIVATE_KEY` `_WEBHOOK_SECRET` `_APP_INSTALL_CALLBACK_URL` | 收 webhook + 安装回调 + 机器人评论发布（App 创建步骤见 [BETA-GUIDE §4](../BETA-GUIDE.md)） |
| GitHub OAuth | `MU_GITHUB_OAUTH_CLIENT_ID/_SECRET/_CALLBACK_URL` | 用户登录。**注意 GitHub App 与 OAuth App 是两个应用，都要建** |
| LLM | `MU_LLM_PROVIDER`（=openai_compatible）`_BASE_URL` `_MODEL` `_API_KEY`，可选 `MU_LLM_ALLOWED_MODELS` | 第二阶段模型审查的出站配置（不填则仅静态规则审查） |

**镜像引用**：`docker-compose.yml` 中的 console `image:` 已 pin 到 rc.35.6-rc2 官方 digest
（`sha256:6d25fd1e…b4cb`；见 Release `v0.2.0-beta.6-rc.35.6-rc2` 与官网下载页身份链）——如需其他版本，
部署前改成**对应 Release 提供的镜像 digest**（digest 不可变，勿用浮动 tag）。
pgvector 与 bge-sidecar 的 digest 已钉死。

### 2. 启动与验证

```bash
docker compose --env-file .env up -d
curl -fsS http://127.0.0.1:${CONSOLE_PORT:-48500}/api/health
# 期望：{"ok":true,"version":"v0.2.0-beta.6-rc.35.6-rc2","mu_schema_ready":true,...}
# （version/git_commit 应与 Release 冻结身份一致：rc.35.6-rc2 / ea72400af462b9f6dbf05318b1ef0ebb3bcc81d1）
```

- 首次启动 console 自动执行 mu schema 迁移（幂等、只前进，当前至 **v29**）。
- 全量核查（推荐，脚本化验收退出码 0=通过）：

```bash
node scripts/delivery/diagnose.mjs --env deploy/selfhost/.env \
  --base-url http://127.0.0.1:48500 --pg-container mergepilot-postgres-1
```

> 诊断工具是零依赖单文件，纯镜像部署用户可从发行源码包中取
> `scripts/delivery/diagnose.mjs` 直接运行（node >= 20）。

### 3. 首个平台管理员（一次，DB 直授）

OAuth 登录只接受**事先创建的邀请**，且邀请永不授予 `platform_admin`——首个平台管理员
由部署者数据库直授。完整 SQL 块、硬校验与回退见
[`deploy/selfhost/README.md`「首个平台管理员初始化（生产）」](../../deploy/selfhost/README.md#首个平台管理员初始化生产)。

验证：该账号登录后右上角显示「平台管理员」；`GET /api/mu/session` 的 `role` 为 `platform_admin`。

### 4. 邀请成员并接入仓库

1. 管理员在「组织与接入 → 成员与邀请」按受邀人 **GitHub 数字 user id**（或 Gitee 数字 id）创建邀请，复制邀请链接发给对方。
2. 受邀人从邀请链接登录，确认顶栏身份/角色。
3. **GitHub**：maintainer 在仓库页「安装 / 更新 GitHub App」→ 选择账号与仓库 → 回跳后选择仓库**绑定**。
4. **Gitee**：仓库页建立 Gitee 连接（使用部署级 `MU_GITEE_PAT`）并绑定仓库。

验证 webhook 端到端：向已绑定仓库开一个测试 PR（或在 GitHub App 高级页 **Redeliver** 一条历史投递），
约 1 分钟内「运行记录」出现该 PR 的 run，状态推进到 `REVIEWED`，发现列表可见（`source=precheck` 静态规则发现）。

**至此目标一完成**：webhook→快照→静态规则审查→发现落库→人工查看，全链可用。

## 目标二：启用 LLM 第二阶段审查（模型审查）

前置：目标一完成；`MU_LLM_*` 四项已配置。

1. **模型白名单**：默认 `deepseek-flash,deepseek-chat`。用其他模型须设置
   `MU_LLM_ALLOWED_MODELS=<逗号分隔列表>` 并重启。
2. **运行策略**（platform_admin）：「组织与接入」运行策略面板 → `llm_assist` + 选择模型 → 保存。
   策略只影响新 run（run 创建时冻结快照）。
3. **出站授权**（platform_admin）：出站策略设为 `external_api`（声明 provider/model）→
   提交**同意记录**（`PUT /api/mu/review-policy` + `POST /api/mu/review-policy/consent`）。
   两者缺一 → 出站否决层 deny（`EGRESS_*` 稳定机器码），LLM 阶段如实标记未执行，静态结果不受影响。

验证：新开/更新一个测试 PR → run 详情出现 `source=reviewer` 的模型发现（或明确的模型调用留痕）；
若模型审查未执行，失败码（如 `LLM_POLICY_ENV_MISMATCH` / `EGRESS_DENIED:*`）会如实透出——按
[TROUBLESHOOTING「T-MODEL」](TROUBLESHOOTING.md#t-model--llm-第二阶段未执行--模型授权问题)处理。

> 语义注意：模型发现是**模型判断，非实际测试**；「零发现」也会执行第二阶段（两阶段解耦，
> 不以静态结果短路模型审查）。

## 目标三：启用修复验证（审批门 → 补丁 → 可应用性 → 修复测试）

前置：目标一完成；准备一个 AgentTeams 运行面（controller + Matrix，见
[`deploy/agentteams-beta/RUNBOOK.md`](../../deploy/agentteams-beta/RUNBOOK.md)）。

1. **执行器**（修复轮运行面，fail-closed——未配置则修复轮拒绝执行，审查主链不受影响）：

```bash
MU_EXECUTOR=agentteams
MU_AGENTTEAMS_BASE_URL=http://<controller>:8090
MU_AGENTTEAMS_TOKEN=<部署级 token>
MU_AGENTTEAMS_RUNTIME=copaw          # 与目标集群 runtime 对齐
MU_AGENTTEAMS_MODEL=deepseek-chat    # 与目标集群模型对齐
MU_AGENTTEAMS_MATRIX_URL=http://<controller>:6167
MU_AGENTTEAMS_MATRIX_USER=<admin>
MU_AGENTTEAMS_MATRIX_PASSWORD=<部署级凭据>
```

2. **修复测试命令**（实际修复测试的命令**唯一来源**；绝不取自模型输出）：

```bash
# JSON 映射；key 为 "provider:owner/repo"（兼容 "owner/repo"）
MU_FIX_TEST_CMDS={"gitee:acme/widget":"npm test","github:acme/widget":"cargo test"}
```

未配置 → 修复测试如实登记「未执行」（可应用性检查不受影响）。

3. **走一遍修复链**：测试 PR 制造一个 P0/P1 缺陷 → 审查后 maintainer 在「审批」页逐票批准
   （run 内全部 P0/P1 票批准才放行）→ 修复轮（dry-run，零远端写入）→ run 详情查看：
   - 建议补丁（统一 diff；版本化，原始版本永不覆盖）；
   - **验证三分**：格式与路径检查 / 真实可应用性（五态）/ 实际修复测试（pre 失败→post 通过）。

验证：`docker exec <console容器> env | grep -c MU_AGENTTEAMS`（配置已注入）；
run 详情页三个验证区块齐备且状态与实际一致。

## 目标四：人工确认提交与合并 —— rc.35.5 起可用

> ⚠️ **首用户前置警示（先读）**：合并资格门要求**当前 head 的真实 CI 通过**。目标仓库
> 如果没有配置任何 CI（combined status 与 check runs 两面全空），合并会**恒被
> `ci_not_configured` 阻塞**——这是有意设计（系统不冒充"CI 通过"），不是故障。
> 接入仓库前先确认它有可用的 CI（如 GitHub Actions）；没有 CI 的仓库可以使用到
> 「审批 → 补丁 → 预览并确认应用 → 自动重审」为止，合并需要先补 CI。

- **确认应用**（`apply_fix`）与**确认合并**（`merge_pr`）已在 main 实现
  （PR #455-#457：preview/confirm 双端点 + 前端两步确认 + 推送式条件更新），
  随 rc.35.5 发布生效。依赖：`apply_fix`/`merge_pr` 权限（maintainer + active 仓库绑定）
  + 机器人凭据（GitHub App installation token / Gitee PAT，部署级 env）。
- 边界保持：每步 preview→confirm 两次确认；非快进推送被远端拒绝（不覆盖他人提交）；
  幂等键防重复写入；平台保护规则照常生效、不被绕过。
- **rc.35.5 收口后语义（#458+可靠性收口）**：
  确认绑定 `expected_remote_head`+`expected_patch_digest`（缺字段/换版/存储篡改三类拒绝）；
  数据库原子认领（并发败方零覆写）；候选 SHA 写前持久化；超时/断连→`result_uncertain`
  →「读回对账」端点定案（`POST /api/mu/apply-operations/:id/reconcile`，幂等，恢复唯一入口）；
  应用成功自动按新 head 入队重审（与 webhook 同消费单元、幂等去重、失败可恢复）；
  合并资格门统一判定（PR 开放+head 一致+mergeable 已知+当前 head 审查完成无未处理 P0/P1
  +当前 head 真实 CI 通过；**mergeable 未知/CI 未配置均不放行**）。
- **GitHub App 权限要求（缺任一即对应能力 unsupported 并在界面报告）**：
  Contents: **Read and write**（应用修复推送）；Pull requests: Read and write（合并）；
  Commit statuses: Read 与 Checks: Read（合并资格门的 CI 双面判定）。
  在 App 设置页 → Permissions 更新后，仓库安装会提示重新授权（Revoke & request）。
- **能力边界**：**当前 Gitee 连接器的 CI 读取不支持**（实测两个 status 形态端点均 404）
  → Gitee 合并资格的 CI 部分恒为 unsupported 阻塞。此为连接器能力结论，不断言 Gitee
  平台整体不存在相关 API；必需检查清单需 admin 读取——由平台在合并时强制，本服务不预判。

## 可选：RAG / cchain（信任链）

- **RAG**：需要 bge-m3 sidecar + 模型卷内的 `manifest.json`（模型安装流程生成，sidecar 以
  `BGE_MANIFEST`/`BGE_MODEL_DIR` 校验）+ `RAGTRIAL_ALLOWED_SCOPES` 等。完整步骤见
  [BETA-GUIDE §8](../BETA-GUIDE.md)。不接 RAG 可整段注释 sidecar，console 对 NOT_WIRED 如实降级。
- **cchain 三缺口**（自托管首次引导常卡点，均为设计行为）：
  1. `fxv.audit_events` 表需手工建表（SQL 见 [selfhost README「配置参考」](../../deploy/selfhost/README.md#配置参考)）；
  2. keystore 首把种子密钥需按 **uid 1000** 属主/权限（目录 770 / 文件 600）预置；
  3. provider attestation 静态端点需自托管 `attestation.json`。
  三键不全 → cchain=BLOCKED（fail-closed 如实呈现，**不影响审查主链**）。

## 运维护护入口

- **日志**：`docker logs -f <console 容器名>`（健康/错误审计亦见 `/api/health` 与 `mu.audit_event`）；
- **问题登记**：仓库 GitHub Issues（附 health 输出、镜像 digest、时间窗）；
- **版本身份**：`/api/health` 的 `version`/`git_commit` 对照对应 Release 的冻结身份。

## 升级与回滚（要点）

```bash
# 升级：改 compose 里 console 镜像 digest → 拉取并滚动（数据卷不动；schema 自动迁移）
docker compose --env-file .env pull console && docker compose --env-file .env up -d
# 回滚：镜像引用改回旧 digest 再 up -d（schema 只前进不降级；升级前 pg_dump 备份）
```

升级后核查：`curl /api/health` 的 `version`/`git_commit` 与发行说明一致；再跑一次诊断工具。

**远端事实不受容器回滚影响**：rc.35.5 起机器人会向你的仓库提交修复 commit、合并 PR——
这些远端提交/合并**不会**因把容器回滚到旧镜像而撤销；远端回退只能在 GitHub/Gitee
手动 revert（见 [RELEASE-rc.35.5.md](RELEASE-rc.35.5.md)）。
