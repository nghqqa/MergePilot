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

## C-8 用户身份与会话（PR 工作台轮新增，2026-09-22）

- **现状**：控制台无任何会话接口（`GET /api/session` = 404，已实测）。前端已实现会话交互结构：
  检查中 / 未登录 / 已登录 / 会话过期（401）/ 服务不可达 五态分离 + 明确标注的"只读演示预览"
  （sessionStorage 标记，非登录、非长期凭证）；登录页不渲染假表单、不模拟登录成功。
- **需要**：`GET /api/session` → 200 `{user: {id, name, login?}}` 或 `{user: null}`；401 = 会话过期；
  403 = 已认证但无权限；登出端点（方案随认证方式定）。
- **认证方案待后端拍板**（GitHub OAuth / 本地账号 / 反向代理身份均可，控制台只消费会话）。
- **边界**：控制台不自建账号密码库；GitHub App 私钥 / installation token 永不进浏览器；
  路由守卫只改善交互，授权以后端为准。

## C-9 仓库列表与访问权限（PR 工作台轮新增）

- **现状**：仓库列表由前端从历史快照推导，页面已标注"历史数据中的仓库"，不虚构连接状态与活跃度。
- **需要**：`GET /api/repos` → `[{owner, name, permission, installation_status, data_mode}]`，
  按当前用户权限过滤（依赖 C-8）。

## C-10 按仓库 PR 分页 + 当前 head 权威（本轮最关键的诚实缺口）

- **现状**：PR 聚合在前端完成（同仓库+PR 编号分组，多历史 head 并存）。因快照无当前 head 权威，
  所有摘要一律降级为"最近记录"，页面不得声称"当前 PR 审查结果"。
- **需要**：
  - `GET /api/repos/{owner}/{name}/prs?page&per_page&q` → `{total, items:[{pr_number, title, github_head_sha, url, latest_run:{run_id, pack_id, verdict, severity, created_at}, ...}]}`（建议按活动时间倒序）；
  - `GET /api/repos/{owner}/{name}/prs/{n}` → 当前 head + 关联 run 列表 + GitHub 状态（开放/合并/关闭）。
- 有了 `github_head_sha` 权威后，前端即可区分"当前 head 结论 / 旧 head 历史"两类展示，替换现有降级口径。

## C-11 审批只读票视图 + 决策接口（对 C-4 边界的更新）

- **更新（2026-09-22 PR 工作台轮）**：控制台已落地审批交互结构，并在**测试数据模式（合成票据 FIXTURE-*，
  本页内存演练，零真实写操作）**下验证四条路径：正常提交 / head 冲突预检拦截 / 过期预检拦截 /
  超时后先查询再确认（不盲目重发）。原 C-4"控制台 V0 不接任何写操作"边界据此更新为：
  **后端决策接口与权限就绪后，控制台可启用真实批准/拒绝**（仍受 D-1/D-2/D-3 约束）。
- **需要**：
  - `GET /api/approvals?status=PENDING` → 五元组（run_id/repo/head_sha/params_hash/指纹）+ TTL + attempt + approved_by（C-4 原需求）；
  - `POST /api/approvals/{id}/decision` `{decision: APPROVED|REJECTED, expected_head_sha, idempotency_key}` → 200 结果 / 409 head 已更新 / 410 票据过期 / 403 无权限；
  - `GET /api/approvals/{id}` → 查询实际结果（请求超时后控制台先查询，不重发）。
- 决策的有效性（head 一致、未过期、操作者权限、幂等）由服务端权威校验；控制台提交的
  `expected_head_sha` 只是客户端预检，不构成授权。

## C-12 站内合并能力（范围变更记录，本轮未实现）

- **范围变更**：用户提出未来希望在控制台直接执行 merge（原口径为"合并仅在 GitHub"）。已记录为范围变更；
  本轮控制台未实现、未调用任何 GitHub merge API、未申请新权限。
- **控制台本轮已做**：PR 详情顶部 GitHub 查看入口；操作区明确"审批与合并是两件事"（审批授权 ≠ 可合并）；
  不放置点击后永远"开发中"的假合并按钮。
- **启用条件（须由后端逐项证明，前端只按能力标识渲染）**：用户对该仓库有合并权限；PR 当前 head 与用户
  确认的 head 一致；GitHub 分支保护 / required checks / review 规则满足；未知或冲突状态不允许绕过；
  合并方法（merge/squash/rebase）由服务端声明可用范围；合并记录可审计；请求超时后可查询实际结果防重发。
- **需要接口（提案）**：`GET /api/repos/{owner}/{name}/prs/{n}/merge-readiness`；
  `POST /api/repos/{owner}/{name}/prs/{n}/merge` `{method, expected_head_sha, idempotency_key}`。
- **边界**：前端绝不仅凭 MergePilot 审查通过或票据 APPROVED 判定允许合并。

## C-13 功能能力标识（PR 工作台轮新增）

- **需要**：`GET /api/capabilities` → `{live, session, approvals_read, approvals_write, merge, ...}`。
  控制台按能力标识渲染入口，避免任何"永远开发中"的假按钮；未声明的能力一律显示"未接入"。
