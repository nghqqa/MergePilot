# MergePilot 能力矩阵（交付口径）

> 版本基线：**rc.35.5**（源码 main@af8b0022，生产镜像
> `sha256:e30419990434c209de8d9de7fc11fc267d8c9e6fe0d99ff242eda69b7a61dd3a`；
> 见 [RELEASE-rc.35.5.md](RELEASE-rc.35.5.md)）。本文描述**当前已上线的实际能力**与各能力的真实依赖；
> 开发中的能力单独标注「开发中」，不与已上线能力混同。
> 依赖符号：env=部署环境变量；DB=数据库表（schema 自动迁移至 v29）；执行器=AgentTeams 运行面；
> 凭据=GitHub/Gitee/模型 key；模型授权=出站策略与同意记录。
> 诊断：各项就绪状态可用 [`scripts/delivery/diagnose.mjs`](../../scripts/delivery/diagnose.mjs) 核查
> （失败项会指向 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 对应条目）。
> 首次使用路径与全流程步骤见 [USER-GUIDE.md](USER-GUIDE.md)；端到端演示材料见
> [DEMO-PR12.md](DEMO-PR12.md)。

## 1. 主链能力一览

| # | 能力 | 状态 | 需要的依赖 | 说明 |
|---|---|---|---|---|
| 1 | PR 事件接入（webhook→快照） | ✅ 已上线 | GitHub App 五项 env + 公网 webhook 入口；Gitee 走部署级 PAT + webhook | HMAC 验签、delivery 一次性去重；job 自动消费循环（默认 45s） |
| 2 | 静态规则审查（precheck） | ✅ 已上线 | 仅 DB | 确定性规则，零网络零模型；发现 `source=precheck` |
| 3 | LLM 第二阶段审查（模型审查） | ✅ 已上线（可选） | env `MU_LLM_*` 四项 + 白名单 `MU_LLM_ALLOWED_MODELS` + 控制台运行策略（`llm_assist`）+ 租户出站策略 `external_api` + 同意记录 + DB `mu.review_policy` | 与静态规则**解耦**：零发现也无条件执行；发现 `source=reviewer`；失败 fail-closed 回落静态结果，绝不误报"模型已执行" |
| 4 | 发现落库与展示 | ✅ 已上线 | DB | 标题/位置/原因/依据/修复建议分列；用户反馈（确认/误报/暂不处理）单独留痕，不回写审查结论 |
| 5 | PR 人工裁定（approve/reject） | ✅ 已上线 | maintainer 角色（`decide_review`） | branch protection 未知 → `422 cannot_conclude_mergeable`（不可下可合并结论） |
| 6 | 修复审批门（P0/P1 票） | ✅ 已上线 | maintainer 角色 + DB `mu.fix_approval` | 逐 finding 具名票；**run 级全批准**才放行修复轮；拒绝/过期/陈旧 → run BLOCKED |
| 7 | 修复轮（建议补丁生成，dry-run） | ✅ 已上线 | 执行器（`MU_EXECUTOR=agentteams` 七项）+ Matrix 三项 | 零远端写入；模型建议永不直接出站 |
| 8 | 补丁版本化 | ✅ 已上线 | DB `mu.fix_patch`（schema v28） | `patch_version`/`patch_digest`/`active`；原始版本永不覆盖，修正链 `correction_of/kind` 留痕 |
| 9 | 真实可应用性检查（五态） | ✅ 已上线 | 容器内 git + 仓库读取凭据（GitHub installation token / Gitee PAT） | `git apply --check` 对准确 head 实测；证据按 digest 绑定 |
| 10 | 实际修复测试（fix test） | ✅ 已上线（可选） | `MU_FIX_TEST_CMDS`（部署级预批准命令映射） | 隔离副本：修复前测试须失败 → 应用补丁 → 修复后须通过；命令**只**来自部署配置 |
| 11 | 审查结果发布到 PR 评论 | ✅ 已上线 | maintainer 角色（`decide_review`）+ 机器人凭据（GitHub App 安装 / Gitee PAT） | 预览→确认两步；发布前远端 head 复核（陈旧拒绝）；幂等原位更新 |
| 12 | 权限可见化（身份/渠道/租户/角色/能力） | ✅ 已上线 | 无额外依赖 | `my_permissions` 服务端投影；页面按能力渲染按钮并给原因 |
| 13 | 应用修复（机器人向 PR 提交修复 commit） | ✅ 已上线（#458 收口） | `apply_fix` 权限（maintainer+active binding）+机器人凭据（GitHub App installation / Gitee PAT） | #454~#458：preview/confirm 两步确认；确认绑定 `expected_remote_head`+`expected_patch_digest`（换版拒绝）；存储字节 digest 复核；数据库原子认领（并发败方零覆写）；两阶段写（候选 SHA 写前持久化）；超时/断连→result_uncertain+读回对账端点（恢复入口）；推送式条件更新（非快进远端拒绝）；幂等防重复 |
| 14 | 新 head 自动重审 | ✅ 已上线（#458 收口） | event_sync 消费单元（与 webhook 同源）；Gitee 需 active forge 连接 / GitHub 需 active binding | apply 成功后自动按新提交入队重审（`trigger_source=apply_fix_reenqueue`）；同 (tenant,repo,pr,head) 已有 run 或待处理 job 即去重；入队失败可恢复（push 不回退，前端提示手动触发兜底） |
| 15 | CI 资格消费（合并资格输入） | ✅ 已上线（#458 收口+#464 双面判定） | CI 状态读取凭据（GitHub installation / Gitee PAT）；目标 head 实际有 CI 在运行 | GitHub 读 combined status **与** check runs 双面（Actions-only 仓库覆盖；完整分页；未知结论阻塞；双面读取任一失败=unknown 阻塞不退化）；Gitee 当前连接器 status 读取不支持（实测 404）=unsupported 阻塞。仓库没有 CI 时合并被资格门如实拦下——有意设计，不是缺陷 |
| 16 | 确认合并（merge_pr） | ✅ 已上线（#458 收口，统一资格门） | `merge_pr` 权限；平台保护规则照常生效（不绕过） | preview/confirm 同一资格判定（confirm 重读远端）：PR 开放+head 一致+mergeable 已知且可合并（null 不放行）+当前 head 审查完成且无未处理 P0/P1（旧 head 证据不可替代）+当前 head 真实 CI 通过；平台保护/审查门合并时由平台强制（Gitee 实测两次真实拒绝如实呈现）；响应丢失→result_uncertain+按 PR 实际状态读回定案；已合并后重复确认幂等返回 |
| 17 | 本地 RAG 检索（可选） | ✅ 已上线 | bge-m3 sidecar + 模型卷 manifest + `RAGTRIAL_*` | 检索结果仅作 reference，不参与风险决策 |

## 2. 权限模型（authz.mjs 为权威，5 角色）

| 能力动作 | contributor | reviewer | maintainer | auditor | platform_admin |
|---|:---:|:---:|:---:|:---:|:---:|
| 查看仓库 / PR 元数据（`read_repository` / `read_pull_request`） | ✓ | ✓ | ✓ | — | ✓ |
| 查看代码内容（`read_code_content`） | ✓ | ✓ | ✓ | — | **—** |
| RAG 检索（`rag_query`） | ✓ | ✓ | ✓ | — | ✓ |
| 触发审查 / finding 反馈（`request_review`） | — | ✓ | ✓ | — | ✓ |
| **裁定与审批（`decide_review`：PR approve/reject、修复票、发布评论）** | — | — | **✓ 仅此角色** | — | — |
| **应用修复（`apply_fix`：机器人提交修复 commit）/ 确认合并（`merge_pr`）** | — | — | **✓ 仅此角色**（另需 active Binding） | — | — |
| 发起受控修复（`request_repair`，需 active Binding） | — | — | ✓ | — | ✓ |
| 绑定 / 解绑仓库 | — | — | ✓ | — | ✓ |
| 查看审计元数据（`read_audit`） | — | — | — | ✓（仅此） | ✓ |
| 成员/租户/实例配置、出站策略与同意（`manage_membership` / `manage_instance`） | — | — | — | — | ✓ |

设计原则：**角色不是单一递增链**——contributor→reviewer→maintainer 逐级递增；
platform_admin 与 auditor 是独立线。**平台管理与审查裁定刻意分离**——platform_admin
也无法审批/发布/应用/合并，且**刻意无 `read_code_content`**（不读代码内容）；
默认拒绝 fail-closed，拒绝原因以稳定机器码留痕（`MU_BINDING_REQUIRED` / `action_not_granted` 等）。
邀请永不授予 `platform_admin`（API/认领守卫/DB CHECK 三层拒绝）。

## 3. 能力 → 依赖对照（env 名以代码事实为准）

| 依赖类别 | 具体项 | 影响的能力 |
|---|---|---|
| DB schema | v27（review_output/fix_patch/finding_feedback/pr_publish）、v28（补丁版本化列）、v29（`mu.fix_apply_operation`：应用修复与合并的操作记录/幂等键/候选 SHA/result_uncertain）；启动自动迁移 | #4–#16 |
| env | `POSTGRES_PASSWORD`、`CONSOLE_SESSION_SECRET` | 全部 |
| env | `MU_GITHUB_APP_ID/_APP_SLUG/_PRIVATE_KEY/_WEBHOOK_SECRET/_APP_INSTALL_CALLBACK_URL` | #1 #11（GitHub） |
| env | `MU_GITHUB_OAUTH_CLIENT_ID/_SECRET/_CALLBACK_URL` | 登录（GitHub） |
| env | `MU_GITEE_PAT` | Gitee 仓库连接 / 可应用性检查凭据 / #11 #13 #16（Gitee） |
| env | `MU_GITEE_OAUTH_CLIENT_ID/_CLIENT_SECRET/_CALLBACK_URL` | 登录（Gitee，可选；缺任一项登录页不渲染 Gitee 入口） |
| env | `MU_GITEE_OAUTH_CLIENT_ID/_SECRET/_CALLBACK_URL` | 登录（Gitee） |
| env | `MU_LLM_PROVIDER/BASE_URL/MODEL/API_KEY`、`MU_LLM_ALLOWED_MODELS` | #3 |
| env | `MU_EXECUTOR` + AgentTeams 七项（base_url/token/runtime/model + Matrix url/user/password） | #7 |
| env | `MU_FIX_TEST_CMDS`（JSON 映射 `provider:owner/repo → 命令`） | #10 |
| env | `MU_JOB_CONSUMER_ENABLED`（默认开）/ `MU_JOB_CONSUMER_INTERVAL_MS`（默认 45000） | #1 自动消费 |
| 控制台策略 | 「运行策略」= llm_assist + 模型 ∈ 白名单；「出站策略」= external_api + 同意 | #3 |
| 凭据 | GitHub App 安装（installation active + binding active） | #1 #9 #11 #13 #15 #16 |
| GitHub App 权限 | 审查只读面：Metadata/Contents/Pull requests/Checks/Commit statuses/Administration 读；写侧（缺任一即对应能力 unsupported 并在界面报告）：Contents **Read and write**（#13 推送）、Pull requests **Read and write**（#16 合并）；CI 读面：Commit statuses 读 + Checks 读（#15 双面判定） | #1 #9 #11 #13 #15 #16 |
| 执行器 | AgentTeams controller + Matrix worker room | #7 |

## 4. 事实源文件（本文各条目的代码出处）

| 条目 | 代码 |
|---|---|
| 角色/动作/Binding 门 | `console/backend/lib/multiuser/authz.mjs` |
| 审查链（静态+LLM 解耦、source 标注、stale head） | `console/backend/lib/multiuser/review-service.mjs` |
| LLM 策略合成与白名单 | `console/backend/lib/multiuser/agent-policy.mjs`、`agents/llm.mjs` |
| 出站否决层（EGRESS_* 六理由） | `console/backend/lib/multiuser/review-arch.mjs`、`review-policy-store.mjs` |
| 审批门（票/CAS/消费/STALE） | `console/backend/lib/multiuser/fix-approval.mjs` |
| 补丁五态 / fix test / 预批准命令来源 | `patch-applicability.mjs`、`patch-test.mjs`、`agents/patch-test-cmd.mjs` |
| 发布（head 复核/幂等/双平台） | `console/backend/lib/multiuser/publish.mjs`、`artifacts.mjs` |
| 应用修复/合并/读回对账（preview-confirm 绑定、原子认领、两阶段写、统一资格门、CI 双面判定） | `console/backend/lib/multiuser/fix-apply.mjs`、`remote-write.mjs`（端点在 `api.mjs`） |
| schema v29 | `console/backend/lib/multiuser/schema.mjs` |
| webhook 自动消费 | `console/backend/lib/multiuser/job-consumer.mjs` |
