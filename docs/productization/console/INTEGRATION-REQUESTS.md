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
