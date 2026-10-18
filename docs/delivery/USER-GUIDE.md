# MergePilot 用户指南（rc.35.6-rc3 口径）

> 面向受邀成员与维护者的日常使用。部署与配置见 [DEPLOY-GUIDE.md](DEPLOY-GUIDE.md)；
> 出错先查 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)；能力事实锚点见
> [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md)。
>
> 版本范围（2026-10-18）：官方**预发布** rc.35.6-rc3 镜像对应源码 `8f8cfb1f3b81463a33c88e49202030e7abaeb43d`，
> 已包含恢复提示、审批上下文、bootstrap 角色保持、总览与远端 head 未知态等修正，以及发布确认门
> 与 CVE-2026-85091（zlib）闭合；其发布之后合入 main 的修正尚未进入官方镜像。
> 下文流程能力以 rc.35.6-rc3 为基线，其后修正见 §6；
> 官网配套入口为 [首次使用](../../website/user-guide.html)，制品区别见
> [下载与发布范围](../../website/downloads.html#release-scope)。
>
> 边界一句话：**系统不自动改代码、不自动合并、不设 required check。远端写共三类——
> 发布审查评论、应用修复（机器人提交）、合并 PR——每类都由维护者逐次「预览 → 确认」
> 才会执行，写入身份固定为机器人（不是你的个人账号），全程审计留痕。**

## 0. 从哪里开始（两条首次使用路径）

### 路径 A：受邀用户使用已有实例

管理员已部署并邀请你。你要做的只有四步：

| 步 | 谁能操作 | 页面入口 | 前提 | 预期结果 | 被阻塞时 |
|---|---|---|---|---|---|
| A1 邀请登录 | 被邀请成员 | 管理员提供的 `/login?invite=<id>` 链接 → 选 **GitHub** 或 **Gitee** | 管理员已按你的**平台数字 id** 创建邀请且未过期 | 落回 Console，顶栏显示「身份 · 组织 · 角色」 | `not_invited` / `invitation_expired` 等：邀请与到访账号不一致或已失效——找管理员重建，见 [T-OAUTH](TROUBLESHOOTING.md#t-oauth--登录--oauth-回调问题) |
| A2 确认角色 | 全部成员 | 顶栏身份区 | 已登录 | 角色与邀请授予一致（角色决定按钮可用性） | 显示的角色不符：找管理员核对邀请角色；邀请不能授予 platform_admin |
| A3 访问已绑定仓库 | 全部成员 | 左侧「**仓库**」 | maintainer 已完成 GitHub App 安装 + 仓库绑定（或 Gitee 连接绑定） | 仓库列表可见，绑定状态 `active` | 列表为空：仓库尚未绑定或你不在该租户——找维护者，见 [T-BINDING](TROUBLESHOOTING.md#t-binding--仓库绑定问题) |
| A4 查看 PR 审查结果 | 全部成员 | 「**仓库**」→ PR 详情，或「**运行记录**」→ run 详情 | 该 PR 有审查 run（新开/更新 PR 自动触发；也可手动触发） | 发现列表、建议补丁、验证结果按来源分列可见 | 无 run：确认 PR 属于已绑定仓库、webhook 可达，见 [T-WEBHOOK](TROUBLESHOOTING.md#t-webhook--webhook-未到达--事件不进管线) |

贡献者到此为止；触发审查可用 reviewer / maintainer / platform_admin；审批/发布/应用/合并仅维护者（见 §1）。

### 路径 B：自托管管理员

你要搭建自己的实例并让团队用起来。**这不是"几分钟一键完成"的托管服务**——
Console、数据库、GitHub App、OAuth、LLM 端点都由你自己部署与运营，按能力逐项启用、
每步带验证命令。入口：[DEPLOY-GUIDE.md](DEPLOY-GUIDE.md)（下表为目标索引）：

| 顺序 | 目标 | 启用什么 | 指南 |
|---|---|---|---|
| B1 | 最小部署 | webhook→静态规则审查→发现落库→人工查看 | [DEPLOY-GUIDE 目标一](DEPLOY-GUIDE.md#目标一最小部署先能完成审查)（依赖准备 → `.env` → 启动验证 → **首个平台管理员 DB 直授** → 邀请成员与仓库接入） |
| B2 | 模型审查（可选） | LLM 第二阶段审查 | [DEPLOY-GUIDE 目标二](DEPLOY-GUIDE.md#目标二启用-llm-第二阶段审查模型审查)（LLM env + 模型白名单 + 运行策略 + 出站策略与同意，四者缺一即如实"未执行"） |
| B3 | 修复验证（可选） | 审批门→补丁→可应用性→修复测试 | [DEPLOY-GUIDE 目标三](DEPLOY-GUIDE.md#目标三启用修复验证审批门--补丁--可应用性--修复测试)（AgentTeams 执行器七项 env + `MU_FIX_TEST_CMDS` 预批准测试命令） |
| B4 | 应用修复与合并 | 机器人提交 + 新 head 重审 + CI 资格 + 确认合并 | [DEPLOY-GUIDE 目标四](DEPLOY-GUIDE.md#目标四人工确认提交与合并--rc355-起可用)（GitHub App 写侧权限：Contents/ Pull requests 读写；CI 读侧：Commit statuses/ Checks 读。**注意：仓库需有真实 CI——没有任何 CI 的仓库合并会被 `ci_not_configured` 恒阻塞**） |

管理员身份要点：**首个平台管理员由部署者数据库直授**（邀请永不授予 platform_admin，
见 [selfhost README「首个平台管理员初始化（生产）」](../../deploy/selfhost/README.md#首个平台管理员初始化生产)）；
初始化完成后管理员同样受角色矩阵约束——platform_admin 也无法审批或合并。

## 1. 登录与身份：先分清四个概念

| 概念 | 回答的问题 | 在哪里 |
|---|---|---|
| **OAuth 登录** | 你是谁（GitHub/Gitee 身份 → 平台账号） | 登录页；仅受邀成员可登录（无公共注册） |
| **GitHub App 安装** | 机器人能否接入你的仓库（installation） | GitHub 侧安装页 → 回跳 Console 登记 |
| **仓库绑定** | 该仓库授权给本实例哪个租户（binding） | 「组织与接入」仓库绑定面板；状态须为 `active` |
| **角色** | 你能做什么（权限矩阵投影） | 顶栏「身份 · 组织 · 角色」 |

四者独立：有登录不等于有绑定，有绑定不等于有审批权。角色矩阵（权威源
`authz.mjs`，拒绝时按钮置灰并给出原因，这是权限设计不是故障）：

| 能做 | contributor | reviewer | maintainer | auditor | platform_admin |
|---|:---:|:---:|:---:|:---:|:---:|
| 查看仓库 / PR 元数据 | ✓ | ✓ | ✓ | — | ✓ |
| 查看代码内容（`read_code_content`） | ✓ | ✓ | ✓ | — | **—** |
| RAG 检索 | ✓ | ✓ | ✓ | — | ✓ |
| 触发审查 / finding 反馈（`request_review`） | — | ✓ | ✓ | — | ✓ |
| **裁定与审批**（`decide_review`：PR 裁定、修复票、发布评论） | — | — | **✓ 仅此角色** | — | — |
| **应用修复**（`apply_fix`）/ **确认合并**（`merge_pr`） | — | — | **✓ 仅此角色**（另需 active 绑定） | — | — |
| 发起受控修复（`request_repair`） | — | — | ✓ | — | ✓ |
| 绑定 / 解绑仓库 | — | — | ✓ | — | ✓ |
| 查看审计元数据（`read_audit`） | — | — | — | ✓（仅此） | ✓ |
| 成员/租户/实例配置、出站策略 | — | — | — | — | ✓ |

**角色不是单一递增链**：contributor→reviewer→maintainer 逐级递增；**platform_admin
与 auditor 是两条独立线**——platform_admin 管成员/配置/出站策略，**刻意无
`read_code_content`**（不读代码内容）、无审批、无裁定、无发布、无应用/合并权
（平台管理与审查裁定分离；rc.35.5 实例 `my_permissions` 实测投影与 authz.mjs 一致）；
auditor 仅审计元数据。触发审查不要说"reviewer 及以上"——准确说法是
**reviewer / maintainer / platform_admin 三角色可用**。需要审批/合并能力，
必须用 maintainer 账号操作。

## 2. 仓库接入

- **GitHub**：maintainer 在「组织与接入」→「安装 / 更新 GitHub App」→ 在 GitHub 选择
  账号与仓库 → 回跳 → 选择仓库**绑定**。
  - **审查所需（只读面）**：Metadata / Contents / Pull requests / Checks / Commit
    statuses / Administration 的读权限。
  - **应用修复与合并所需（写侧，缺任一则对应能力在界面报告 unsupported）**：
    Contents **Read and write**（机器人推送修复提交）、Pull requests **Read and
    write**（合并）。在 App 设置页补权限后，仓库安装会提示重新授权。
  - 详见 [CAPABILITY-MATRIX §1](CAPABILITY-MATRIX.md) 与 [BETA-GUIDE §4](../BETA-GUIDE.md)。
- **Gitee**：仓库页建立 Gitee 连接（部署级 `MU_GITEE_PAT`）→ 绑定仓库。Gitee 登录走
  Gitee OAuth（`MU_GITEE_OAUTH_*` 三项）。
- 绑定后，PR 新开/更新（webhook）自动触发审查，无需手动点。手动重审：PR 详情页
  「发起审查」（reviewer / maintainer / platform_admin）。
- 一个 GitHub 仓库全局只能被一个租户绑定；App 被卸载 → installation 与全部绑定自动
  `revoked`（fail-closed），重装后重新绑定。

## 3. 完整使用路径：从 PR 事件到合并与审计

以下 10 步是一条 PR 的完整生命周期。**每一步都有明确的责任人与确认点，系统不会
跳过任何人工确认**。

### 步骤 1：PR 事件接入（自动）

- **谁能操作**：无需操作（webhook 自动）；reviewer / maintainer / platform_admin 可手动「发起审查」补触发。
- **页面入口**：触发后「运行记录」出现新 run；PR 详情页可从「仓库」进入。
- **前提**：仓库绑定 `active`；GitHub App 安装且 webhook 公网可达（Gitee 走部署级
  PAT + 仓库 hook）。
- **预期结果**：约 1 分钟内出现 run，状态开始推进。
- **阻塞原因与下一步**：无 run → 查 [T-WEBHOOK](TROUBLESHOOTING.md#t-webhook--webhook-未到达--事件不进管线)；
  消费循环被关闭（`MU_JOB_CONSUMER_ENABLED=0`）→ 找管理员恢复。

### 步骤 2：静态 / 模型审查（自动）

- **谁能操作**：自动执行。模型阶段需部署方已启用（B2）。
- **页面入口**：run 详情 → 发现列表，来源分列 `precheck`（静态规则）与 `reviewer`
  （模型发现）。
- **预期结果**：静态发现零延迟落库；模型发现独立标注——模型阶段未执行/失败会**如实
  标注**（如 `EGRESS_DENIED:*`），绝不冒充"模型已执行"，静态结果不受影响。
- **阻塞原因与下一步**：模型未执行 → 按 [T-MODEL](TROUBLESHOOTING.md#t-model--llm-第二阶段未执行--模型授权问题)
  处理；这不影响静态发现照常可用。

### 步骤 3：查看发现（全体成员）

- **页面入口**：「运行记录」→ run 详情（或「待处理」页聚合入口）。
- **预期结果**：每条发现含标题/位置/原因/依据/修复建议；你可为发现提交反馈
  （确认/误报/暂不处理，单独留痕，不回写审查结论）。
- **阻塞原因**：无（查看不受角色限制，auditor 除外——仅审计元数据）。

### 步骤 4：维护者批准修复生成（仅 maintainer）

- **页面入口**：「**审批**」页（P0/P1 逐条具名票，带 finding/位置/head/有效期）。
- **前提**：run 产生了 P0/P1 发现（低风险发现不产生票，不阻塞）。
- **预期结果**：逐票批准或驳回；**run 内全部 P0/P1 票批准后**修复轮才启动（没有
  "全部批准"捷径）。修复轮产出建议补丁与验证证据（此阶段仍不写远端）。
- **阻塞原因与下一步**：任一票驳回 / 过期 / head 已变化（STALE）→ run BLOCKED
  （fail-closed），且**永不自动恢复**——PR 更新出新 head 重新审查才会产生新票。
  票状态语义见 [TROUBLESHOOTING](TROUBLESHOOTING.md) 与 [ACCEPTANCE-CHECKLIST §6](ACCEPTANCE-CHECKLIST.md)。

### 步骤 5：查看补丁与验证证据（全体成员可读）

- **页面入口**：run 详情产物面板——建议补丁（统一 diff，版本化）、验证结果、
  Markdown/JSON/`.patch` 下载（下载的即检查所验证的同一版本，文件头含 digest）。
- **预期结果——验证三分，通过≠通过≠通过**：

| 分项 | 回答的问题 | 状态语义 |
|---|---|---|
| ① 格式与路径检查（结构级） | diff 格式合法、触及文件在变更集内？ | 通过 ≠ 可应用 |
| ② 真实可应用性 | 对准确 head 的 `git apply --check` 实测过吗？ | 五态：**通过 / 失败 / 未执行 / 未验证（环境不可用）/ 陈旧**；证据按补丁 digest 绑定，补丁更新后旧证据自动失效 |
| ③ 实际修复测试 | 隔离副本中：修复前测试失败 → 应用补丁 → 修复后通过？ | 需部署方预配置 `MU_FIX_TEST_CMDS`；未配置时如实"未执行" |

  记忆口诀：**可应用 ≠ 行为正确；未执行 ≠ 不适用**。可应用性通过只说明补丁能打上，
  **不能证明修复正确**；修复正确性的证据是 ③（有测试命令时）。
- **阻塞原因与下一步**：补丁不可应用 → 看失败原因；修正闭环会产出新版本
  （`correction_of` 链），页面/发布统一取 `active` 版本，原始版本保留可追溯。

### 步骤 6：预览并确认应用——机器人提交修复（仅 maintainer + active 绑定）

- **页面入口**：PR 详情「**应用修复与合并**」区块 →「**① 预览并应用修复**」→
  预览抽屉确认（确认必须从预览发起，携带预览所示的远端 head 与补丁 digest）。
- **前提**：本轮 run 已产出补丁；你是 maintainer；仓库绑定 active；机器人凭据可用
  （GitHub App installation / Gitee PAT）。
- **预期结果**：机器人以自己的身份（MergePilot GitHub App / MergePilot Gitee Bot）
  向 PR 源分支**推送式**新增一个修复提交（非快进推送会被远端拒绝，不会覆盖他人提交）；
  操作记录留痕（含候选提交 SHA）。同一 run/补丁重复确认幂等返回，不会重复提交。
- **阻塞原因与下一步**：

| 机器码 | 含义 | 处理 |
|---|---|---|
| `invalid_confirm_fields` | 未从预览发起确认 | 回预览重新走两步 |
| `patch_changed` | 预览后补丁被修正闭环换版 | 重新预览（确认的 digest=预览所示版本） |
| `stale_head` / `head_moved_since_preview` | 远端 head 已推进 | PR 更新后重新审查，再走本步 |
| `operation_in_progress` | 同一操作正在执行 | 稍后刷新查看，不会重复写 |
| GitHub 403 "Resource not accessible by integration" | App 缺 Contents: Read and write | 管理员在 App 设置补权限后重试，见 [T-APPLYMERGE](TROUBLESHOOTING.md#t-applymerge--应用修复--合并-pr-问题rc355) |

  **超时/断连 → `result_uncertain`：必须先「读回对账」，禁止盲目重试**，见 §4。

### 步骤 7：新 head 自动重审（自动）

- **谁能操作**：自动（应用成功即按新提交入队重审，与 webhook 同一消费单元、幂等
  去重）；入队失败时 PR 详情会提示用「发起审查」手动兜底（推送不回退）。
- **页面入口**：「运行记录」出现新 head 的新 run（`trigger_source=apply_fix_reenqueue`）。
- **预期结果**：新 head 独立走完整审查——**旧 head 的批准、补丁验证、CI 结论都不能
  替代新 head 的证据**。
- **阻塞原因与下一步**：新 run 发现新问题 → 回到步骤 4（新一轮审批）。

### 步骤 8：新 head 的真实 CI（自动读取，作为合并资格输入）

- **谁能操作**：CI 由你的平台跑（GitHub Actions 等）；MergePilot 只读取结果。
- **页面入口**：合并预览的资格条件区显示 CI 状态。
- **预期结果**：GitHub 侧读取 combined status **与** check runs 双面（Actions-only
  仓库也覆盖）；状态语义：`pass` 通过 / `pending` 运行中 / `failed` 失败 /
  `not_configured` 该 head 没有任何 CI / `unsupported` 无读取权限（如 Gitee 当前
  连接器） / `unknown` 读取不完整（网络失败等）。**除 pass 外一律阻塞合并并给出原因**。
- **阻塞原因与下一步**：CI 未配 → 仓库需要先有真实 CI（系统不冒充"CI 通过"）；
  pending → 等待；failed → 修复代码；unsupported/unknown → 见
  [T-APPLYMERGE](TROUBLESHOOTING.md#t-applymerge--应用修复--合并-pr-问题rc355)。
  **审查完成 ≠ CI 通过**——两件事分别判定。

### 步骤 9：预览并确认合并（仅 maintainer）

- **页面入口**：PR 详情「**应用修复与合并**」区块 →「**② 预览并合并 PR**」→
  预览抽屉「**确认合并 PR**」（可选 merge/squash/rebase，默认 merge；确认必带
  预览所示 head）。
- **前提——统一资格门（preview 与 confirm 同一判定，confirm 时重读远端）**：

  1. PR 仍开放且 head 与预览一致；
  2. mergeable 已知且可合并（**未知不放行**，冲突不放行）；
  3. **当前 head** 存在审查 run 且状态为 REVIEWED/COMPLETED/VERIFIED，无未处理
     P0/P1（旧 head 的证据不可替代）；
  4. **当前 head 真实 CI 通过**（步骤 8）。

- **预期结果**：条件合并执行，merge commit 留痕；审计记录 `MU_PR_MERGED`。
  平台自身的分支保护/必需检查照常在合并时由平台强制——**本服务不绕过**（Gitee
  实测会拒绝"未通过设置的审查/测试"并如实呈现）。
- **阻塞原因与下一步**：资格门给具体原因（`ci_pending` / `ci_not_configured` /
  `blocking_findings_open` / `mergeable_unknown` / PR 已关闭等），按提示补齐后重试；
  **Gitee 当前连接器 CI 读取不支持 → Gitee 仓库的合并会被恒久阻塞**（连接器能力
  边界，不扩大为 Gitee 平台整体结论）。
- **响应丢失 → `result_uncertain`**：同 §4，按 PR 实际状态读回定案；已合并后重复
  确认幂等返回。

### 步骤 10：远端结果与审计

- **页面入口**：GitHub/Gitee 上核对（机器人评论、机器人提交、merge commit）；
  Console「组织与接入」审计区（auditor/platform_admin 可查审计元数据）。
- **预期结果**：每个远端写动作都有对应审计事件（如 `MU_FIX_APPLIED`、
  `MU_REVIEW_REENQUEUED_AFTER_APPLY`、`MU_PR_MERGED`），含操作者、时间、对象标识。
- **重要**：**回滚应用镜像不能撤销已发生的远端提交或合并**——远端回退只能在
  GitHub/Gitee 手动 revert。审计与远端事实互为对账依据。

## 4. result_uncertain：先读回对账，禁止盲目重试

推送/合并请求超时或响应丢失时，系统不知道远端是否已落地，操作记录标记
`result_uncertain`（候选提交 SHA 已持久化）。此时：

1. **唯一正确动作**：PR 详情「应用修复与合并」区 → 历史操作中该操作的「**读回对账**」
   按钮 → 系统读远端实际状态定案（已落地 → committed/merged；未落地 → failed，
   可安全重试；证据不足 → 保持 uncertain 并如实说明）。
2. **禁止**不经对账直接重试——可能产生重复提交/合并。刷新页面、重启服务都不会
   清除该状态（防重复写设计），对账是唯一出口，无超时自动解锁。
3. 直接调 confirm 端点会被拒（`result_uncertain_requires_reconcile`）。

## 5. 发布审查评论到 PR（仅 maintainer）

入口：run 详情产物面板「**预览并发布到 PR**」。

1. **预览**（只读）显示：目标平台与 PR、审查所用 head、**远端当前 head**、
   **写入身份**（MergePilot GitHub App / MergePilot Gitee Bot——不是你的个人账号）、
   评论全文。
2. 点「确认发布」才真正写出。发布与审批同权（`decide_review`，仅 maintainer）；
   后端为最终授权方。
3. 幂等语义：同一 run 重复发布 = **原位更新**机器人自己的既有评论（不刷屏、
   不覆盖人工评论）。
4. head 已推进 → 拒绝发布（409 `head_moved`）：陈旧结论不发布，PR 更新后重新审查。
5. 远端超时/结果不明 → `unconfirmed`：先去 PR 页核实评论是否已写出，**不要盲目重发**
   （同 run 重发幂等）。

## 6. 常见状态速查

rc.35.5 之后加入的修正（**已包含于 rc.35.6-rc3 官方预发布镜像**；其后合入 main 的修正按同口径核对）：

- `BLOCKED/no_repair_pending` 按真实 run 状态及票据/执行记录解释原因。同 head 重复触发
  复用原 run；需要恢复时修正代码、推送新 commit 后重新审查。仅当新 head 再现 P0/P1
  才生成新审批票，旧票不重批。
- 远端 head 区分匹配、已变化、无法核实；`remote_unreachable` 是读取失败，不能解释为
  冲突、CI 失败或 head 已变化。检查机器人凭据/权限/连接后重新预览，错误码用于排查。
- 审批区补平台/仓库/PR 上下文，区分当前与票据绑定 head；终态票无重批入口。
- MU 数据源的「待审批票」只含 PENDING，失败/卡住的 run 仍需在总览查看；
  「最近记录的 head」未必是远端最新版本。

| 状态 | 含义 | 处理 |
|---|---|---|
| 按钮置灰 | 无对应权限 / 历史 run / 无补丁 / 未绑定 | 悬停看原因（如「当前角色无 decide_review」）；用维护者账号或切最新 run |
| 「历史运行，仅查看」 | 产物面板默认显示最新 run | 发布/应用/合并需要最新 run——点「查看最新 run」 |
| head 已变化 | 远端已推进，结论基于旧版本 | PR 更新后重新审查；旧结论不可发布/应用/合并 |
| 可应用性「未验证（环境不可用）」 | 检查环境不可用，如实不误报 | 稍后重试；不影响其余产物 |
| 修复测试「未执行」 | 部署未配置预批准命令 | 管理员按 [DEPLOY-GUIDE 目标三](DEPLOY-GUIDE.md#目标三启用修复验证审批门--补丁--可应用性--修复测试)配 `MU_FIX_TEST_CMDS` |
| 补丁「不可应用」 | 真实 git apply 失败（含修正尝试） | 参考失败原因；原始版本保留可追溯 |
| `result_uncertain` | 远端结果不明 | **先「读回对账」**，见 §4 |
| 合并 `merge_not_eligible` | 资格门给出具体原因 | 按原因补齐（CI/审查/mergeable/PR 状态） |
| 模型审查未执行 | 出站策略/同意/白名单未就绪 | 见 [T-MODEL](TROUBLESHOOTING.md#t-model--llm-第二阶段未执行--模型授权问题) |

## 7. 能力边界与常见误解

- **批准生成补丁 ≠ 发布评论 ≠ 应用补丁 ≠ 合并 PR**：四个动作各自独立确认、各自授权
  （前三者为 `decide_review`/`apply_fix`，合并为 `merge_pr`，均仅 maintainer）。
- **可应用性通过 ≠ 修复正确**：②只证明补丁能打上；正确性看 ③ 的修复前/后测试证据
  与你自己的复核。
- **审查完成 ≠ CI 通过**：审查与 CI 是两个独立事实轴；合并资格门两者都要。
- **旧 head 的审查与验证不能证明新 head 合格**：head 一变，一切证据重新积累。
- **`result_uncertain` 必须先读回对账**：盲目重试可能重复写远端（§4）。
- **容器/镜像回滚不能撤销远端事实**：已推送的提交、已合并的 PR 只能在平台上 revert。
- **Gitee 的边界只属于连接器**：当前 Gitee 连接器不支持读取 CI 状态 → Gitee 仓库
  合并被阻塞。这是连接器能力结论，不是"Gitee 平台不能做 CI"的判断。
- **模型评测结果有时效与样本范围**：固定评测集上的结论不代表你的仓库场景；不要外推
  成"准确率提升 X%"式承诺。

## 8. 截图说明

本指南暂不内嵌截图。所需截图清单（真实页面 + 脱敏后补入）：登录页（邀请入口）、
顶栏身份区、PR 详情发现列表、「应用修复与合并」区块（含「读回对账」）、合并预览
资格门。在取得并完成脱敏（隐去账号/组织/租户标识）前，以上位置以文字描述为准——
不生成替代性示意图。真实页面原始素材（验收线实测留存，未脱敏、勿直接外发）见
[rc35.5-first-use/evidence/](../acceptance/rc35.5-first-use/FIRST-USE-ACCEPTANCE.md)。
