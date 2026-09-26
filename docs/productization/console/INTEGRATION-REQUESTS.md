# 控制台给后端会话的接口需求（INTEGRATION-REQUESTS）

**发出方**：控制台会话（分支 `feat/admin-console`，worktree `D:\goai\mp-worktrees\console`）
**接收方**：后端可靠性/审批/RAG/版本/成本会话（分支 `chore/backfill-r3-ops`）
**性质**：文件交接（无跨会话通信工具）。以下需求按控制台页面的紧迫度排序；未批复前控制台继续以 snapshot 模式运行，不阻塞。

## C-1 run-manifest 读取（版本清单页实证化）— 最优先

- **需要**：一个只读方式获取桥在派发前写入 MinIO 的 `run-manifest.json`（按 run_id / head_sha 检索）。
- **现状**：历史证据包全部早于 manifest 机制，版本清单页只能显示"未记录"；新 run 产生后这是版本页唯一的实证来源。
- **建议形态**：本地/内网可达的只读端点或对象导出（字段直接采纳 manifest 原文：code/prompt/workers/config/missing[]）。
  控制台承诺：只读展示，不写 MinIO。
- **关联**：后端 STATUS 的 run-manifest 工作项（commit 81e0045）；R5 只读探查已列模型/镜像/Skill 哈希来源（DECISIONS #9）。

## C-2 服务器 PG `github_deliveries` 只读查询（live 运行列表）

- **需要**：只读 SQL 通道（或导出）读取投递台账：`delivery_id, repo, pr_number, observed_head_sha, observed_base_sha, status, error, received_at, claimed_at, processed_at`。
- **用途**：live 模式运行列表（`data_mode: "live"`，与 snapshot 并存、页面明确区分）。
- **前置**：属于既有 R 系列授权范围的服务器访问，控制台不自行 SSH——需后端会话在授权轮次内提供导出或代理。

## C-3 MinIO 项目 meta.json / receipt 只读（live 运行详情）

- 同 C-2 授权范围。字段需求：项目 status/title/project_id、check-run receipt（id/conclusion/时间）。

## C-4 审批门页只读数据（P-1 已拍板：SQLite WAL）

- **更新（2026-09-22）**：后端会话已提交 `260e1f6 feat(approval): SQLite WAL ticket store (P-1 decided) + retry-cost contract` 与 `e29e7aa feat(approval): gate_cli ticket operations`——P-1 落点为 SQLite WAL（非早前预研推荐的 MinIO 方案 B）。本需求不变，存储指向按已拍板实现。
- **需要**：控制台的**只读**票视图：五元组绑定（run_id/repo/head_sha/params_hash/指纹）、状态（PENDING/APPROVED/REJECTED/USED/INVALIDATED）、TTL 剩余、attempt、approved_by。
- **边界**：控制台**不做任何 approve/reject 写操作**——M2 规格要求后端权威校验 + D-1/D-2/D-3 拍板，控制台只渲染与链接。写操作接口即使后端提供，控制台 V0 也不接。

## C-5 usage 数据源（R6 二选一拍板后）

- **需要**：按 run 维度的 token/calls 记录（OTel collector 查询 或 worker 本地台账——R6 待选路）。
- **字段**：run_id、窗口起止、calls、input/cached/output tokens、模型标识；价目表若提供则金额可控展示，否则只显示 tokens（现状：不显示金额）。

## C-6 RAG 索引版本标识

- **需要**：rag-live 运行时暴露当前索引/快照版本（如 corpus hash 或 snapshot id）+ 检索调用的索引版本随 run 记录。
- **现状**：历史包 RAG 调用无版本标识（版本页如实"未记录"）；SYNTHETIC 语料警示已按 data_mode 标注。

## 已由控制台单方面处理、无需后端动作的事项

- 历史证据包的运行索引/证据查看/完整性校验（只读，无共享结构改动）；
- 与 INTEGRATION-AUTH-REQUESTS.md（后端会话的 R1-R6）的关系：C-2/C-3 属其服务器访问授权的只读子集，
  控制台不重复申请、不自行执行；C-1/C-4/C-5/C-6 为新增能力需求。

## C-7 live 模式的 PR 标题与元数据（UI-REVIEW 轮新增，2026-09-22）

- **现状**：snapshot 模式 PR 标题取自包内 PR-METADATA.md `标题：` 行（仅部分包有；列表/详情对无记录包诚实显示 `PR #n`）。
- **需要**：live 模式接入后，run 记录需带 PR 标题（GitHub 只读 API `pulls/{n}.title` 或等价数据源），字段名对齐 `pr_title`。
- **边界**：控制台只展示，不缓存不改写；无标题继续显示 `PR #n`。

## C-8 用户身份与会话（已对齐正式契约——等待实现）

- **状态更新（2026-09-23）**：会话契约已由设计窗口定稿并接受——`API-AUTH-MERGE-V0.md` v2
  （`docs/architecture-audit-20260922` @ **7ccecb9**，接受记录 c664df2）：
  **GitHub OAuth + 服务端会话**（Cookie `mp_session` 为权威）；会话端点 **`GET /api/auth/session`**
  （未登录 401 JSON、服务端不重定向；reason 区分 not_authenticated/session_expired）；
  登录入口 `/api/auth/github/login|callback`（豁免端点）；`POST /api/auth/logout`（CSRF）。
  ~~原"认证方案待拍板"~~ 已删除——不再是用户决策项。
- **前端已完成**：适配层（api-live.js：端点/分类/CSRF 预留）+ 会话状态机
  （authed/anonymous/expired/forbidden/auth_unavailable/not_implemented/unavailable）+ 契约测试。
  当前实测 `/api/auth/session` = 404 → 前端如实显示"等待后端实现与 D-9 配置"，不伪装已登录。
- **阻塞**：后端实现 + D-9（GitHub OAuth App）配置。验收样例见 CONTRACT-ACCEPTANCE-SAMPLES.md §1。

## C-9 仓库列表与访问权限（已对齐——等待实现）

- **对齐（2026-09-23）**：能力与权限查询按契约 v2 落在 `GET /api/me/capabilities?repo=owner%2Fname`
  （installation_state + operations[].allowed/reason），repo 寻址统一 `?repo=` 查询参数；
  仓库级总览由 installation 映射提供（PRODUCT-SCOPE-V0 §3 页面 1）。
- **前端已完成**：URL 构造与编码、能力渲染约定（不可用动作标注原因，不显示假按钮）。
  快照推导的仓库列表继续标注"历史数据中的仓库"，待 C-9 实现后替换为授权列表。
- **阻塞**：后端实现（依赖 C-8 会话）。

## C-10 按仓库 PR 列表 + 当前 head 权威（已对齐契约 v2 §2——等待实现；最关键诚实缺口）

- **对齐（2026-09-23）**：契约 v2 §2 已定 PR 聚合视图——
  `GET /api/pulls?repo=owner%2Fname&state&limit&offset`（一 PR 一行三列分离：
  `current_head_sha` GitHub 实时 / `latest_run` 编排状态 / `latest_result` 最新终态结论 + `stale` 标记）与
  `GET /api/pulls/:prNumber?repo=`（run 执行历史展开 + merge_panel + patch_delivery）。
  ~~此前前端自拟的 `/api/repos/{owner}/{name}/prs` 形状~~ 以契约 v2 为准，不再维护两套。
- **前端已完成**：`api-live.js` 端点适配 + `pr-model.associateCurrentHead()`（结论所属 head==当前 head
  才关联为当前结果；当前 head 进行中→"无完成结果"明确说明；旧 head 结论全部标 stale，不用最近成功掩盖
  当前失败/未完成）+ stale/fixture 形状契约测试。snapshot 现状（无权威 head）维持"最近记录"展示。
- **阻塞**：后端实现；PG 读模型（PgRunStore.get_run/runs_for_pr/get_stages）已具备，
  缺正式 HTTP 接线（FRONTEND-HANDOFF §7）。

## C-11 审批只读票视图 + 决策接口（对 C-4 边界的更新；已对齐——等待实现）

- **更新（2026-09-22 PR 工作台轮）**：控制台已落地审批交互结构，并在**测试数据模式（合成票据 FIXTURE-*，
  本页内存演练，零真实写操作）**下验证四条路径：正常提交 / head 冲突预检拦截 / 过期预检拦截 /
  超时后先查询再确认（不盲目重发）。原 C-4"控制台 V0 不接任何写操作"边界据此更新为：
  **后端决策接口与权限就绪后，控制台可启用真实批准/拒绝**（仍受 D-1/D-2/D-3 约束）。
- **需要**：
  - `GET /api/approvals?status=PENDING` → 五元组（run_id/repo/head_sha/params_hash/指纹）+ TTL + attempt + approved_by（C-4 原需求）；
  - `POST /api/approvals/{id}/decision` `{decision: APPROVED|REJECTED, expected_head_sha, idempotency_key}` + `X-CSRF-Token` → 200 结果 / 409 head 已更新 / 410 票据过期 / 403 无权限；
  - `GET /api/approvals/{id}` → 查询实际结果（请求超时后控制台先查询，不重发）。
- **前端已完成（2026-09-23）**：只读适配器形状（api-live.js `approvalsUrl/approvalUrl/approvalDecisionUrl`）
  与 fixture 演练适配器（ApprovalsPage 内存状态机）**彻底分离**；隔离后端下的
  成功/冲突/过期/结果未知四路径已有测试覆盖。
- 决策的有效性（head 一致、未过期、操作者权限、幂等）由服务端权威校验；控制台提交的
  `expected_head_sha` 只是客户端预检，不构成授权。
- **阻塞**：后端实现（依赖 C-8 会话与 D-1/D-2/D-3 拍板）；本轮真实票据操作保持关闭。

## C-12 站内合并能力（范围变更记录；契约 v2 §3 已定——保持关闭，等待启用条件）

- **范围变更**：用户提出未来希望在控制台直接执行 merge（原口径为"合并仅在 GitHub"）。已记录为范围变更；
  本轮控制台未实现、未调用任何 GitHub merge API、未申请新权限、未新增合并执行代码。
- **对齐（2026-09-23）**：契约 v2 §3 已定站内合并为**目标能力、默认关闭**（发起/二次确认/查询三端点、
  202+幂等返回、409 stale_head、422 github_rules_not_satisfied 不降级、unknown 对账收敛三态）——
  前端将按 `merge_panel.enabled=false` 渲染外链态，不实现执行代码抢跑后端。
- **控制台已做**：PR 详情顶部 GitHub 查看入口；操作区明确"审批与合并是两件事"（审批授权 ≠ 可合并）；
  不放置点击后永远"开发中"的假合并按钮。
- **启用条件（须由后端逐项证明，前端只按能力标识渲染）**：用户对该仓库有合并权限；PR 当前 head 与用户
  确认的 head 一致；GitHub 分支保护 / required checks / review 规则满足；未知或冲突状态不允许绕过；
  合并方法（merge/squash/rebase）由服务端声明可用范围；合并记录可审计；请求超时后可查询实际结果防重发。
- **需要接口（提案）**：`GET /api/repos/{owner}/{name}/prs/{n}/merge-readiness`；
  `POST /api/repos/{owner}/{name}/prs/{n}/merge` `{method, expected_head_sha, idempotency_key}`。
- **边界**：前端绝不仅凭 MergePilot 审查通过或票据 APPROVED 判定允许合并。

## C-13 功能能力标识（已对齐——等待实现）

- **对齐（2026-09-23）**：契约 v2 §1 `GET /api/me/capabilities?repo=owner%2Fname` 已覆盖能力标识
  （installation_state + operations[].allowed/reason/detail/github_url；§0.3 执行时服务端重校验）。
  ~~此前前端自拟的 `GET /api/capabilities` 全局开关~~ 以契约 v2 为准。
- **前端约定**：按 allowed/reason 渲染入口，不可用动作隐藏或标注原因（如 merge_disabled + GitHub 外链）；
  未声明的能力一律显示"未接入"；不出现"永远开发中"的假按钮。

## 恢复条件与验收项（前端视角，2026-09-23 口径修正轮新增）

**总原则**：后端交付后，前端**按最终契约评估适配并重新验收**——不预设"无需前端改动"；
若交付形状与 CONTRACT-ACCEPTANCE-SAMPLES 存在差异，差异记入本文件并由双方确认
（一次性开发适配须明确标记 dev-only，不默认长期维护两套正式接口）。

| 恢复条件 | 依赖 | 前端动作 | 验收项（通过标准） |
|---|---|---|---|
| R-1 正式 PR 聚合端点 `GET /api/pulls?repo=`、`GET /api/pulls/:n?repo=`（C-10） | 后端 HTTP→PG 接线 | sources 声明切 contract；退役 console-pg dev 适配层 | 按 CONTRACT-ACCEPTANCE-SAMPLES §3/§4 逐字段对照；current_head 权威生效（"当前结论/stale/进行中无结果"三类展示正确）；多 head 历史保留；分页 offset 稳定；404/503 语义正确 |
| R-2 审批详情字段补齐 `expires_at`/`params`/PR 关联（C-11 缺口） | 后端审批只读响应扩展 | PgTicketRow 展示 TTL/params/PR（现为"响应未携带"如实标注） | 详情含 TTL 倒计时与五元组完整字段；过期票据在列表/详情均可辨识 |
| R-3 决策接口 head 冲突语义（expected_head 或等价参数） | 后端决策接口定义 | 前端预检恢复 expected_head 提交 + 409 冲突展示 | 409 stale_head 场景 UI 显示"绑定 head 与当前不一致，需重签"，不静默放行 |
| R-4 OAuth 会话（C-8 + D-9） | 后端登录三端点 + 会话 | LoginPage 移除"演示预览为唯一入口"态；登出/过期拦截全启用 | `GET /api/auth/session` 200/401 分类实测；403 not_a_member 页面态；CSRF 头按契约携带；浏览器无 token/PAT 存留 |

通用验收（每次恢复都含）：data_mode/fixture 标识不丢失；live/PG 查询失败不回退私有 snapshot；
跨仓库同编号 PR 不串；64 项既有测试全绿 + 新增契约测试按交付形状补齐。

## 前端现有模式与边界（口径基准，2026-09-23 修正）

| 模式 | 触发 | 读/写边界 |
|---|---|---|
| snapshot（默认/生产现状） | console 后端 health 声明 | 只读（控制台本体仅 GET） |
| contract fixture 页面验收 | dev harness（非生产） | 只读 + 合成数据 |
| console-pg 隔离联调 | dev harness `--pg` + console_pg test-auth | GET 查询 + **审批决策 POST（写仅限隔离 fixture 库票据，X-Test-Principal 主体）**——非只读，不触达真实系统 |

注：dev harness 在 PG 模式下是**透传代理**（GET 查询 + 审批决策 POST），不是"只读反代"——
写操作仅限隔离 fixture 票据，由隔离后端强制主体校验，harness 自身无业务逻辑。
