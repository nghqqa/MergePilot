# M2 门票据存储落点预研（GATE-STORAGE-OPTIONS）

**日期**：2026-09-22 ｜ **性质**：决策支持预研，非决定 ｜ **待用户拍板**（列为 DECISIONS 提案 P-1）

问题：M2-APPROVAL-SPEC 的票据模型（tools/approval/，纯逻辑已完成+87 单测面）需要一个真实存储，使 门 Web 页（签发/批准/拒绝）与执行方（桥，校验后动作）共享同一份票据事实。三个候选：

## 方案 A：服务器 PG approvals 表改造

- **做法**：在服务器 `mergepilot_audit` 库新增/改造 approvals 表（剥离 merge 语义，动作集改 V0 三动作），网关/Web 与桥都走 SQL CAS（`UPDATE ... WHERE status='PENDING'` 天然先到先得）。
- **优点**：CAS 仲裁由 PG 原子性保证，跨进程/跨机正确；与 `github_deliveries` 台账同库，审批与投递可关联查询。
- **缺点**：**违反桥现行约束"不改服务器任何 schema"**（gh_bridge 设计约束第一条）——需要新增授权（相当于 R7）；schema 变更的回退/迁移要按部署规程单独立项。
- **适合**：Controller 接管（cutover）之后——那时服务器 schema 变更授权本来就要发生，可搭车。

## 方案 B（推荐，V0）：MinIO 票据对象 + 单写者仲裁

- **做法**：票据存 `teams/elemiso-team/shared/projects/{proj}/approval-{ticket_id}.json`（与 receipt、run-manifest 同通道、同信任域）；write-once 语义（复用 run-manifest 已验证的 read-compare-refuse 模式）。
- **仲裁**：approve/reject 的先到先得不靠存储原子写，而靠**单一写者**——沿用现有 `send_gate_decision` 通道的信任位置（门决策只能从门入口进入）。V0 无多租户/多门入口，单写者成立；S3 条件写（If-None-Match）作为后续可选加固。
- **优点**：零 schema 变更（不触 R 约束）；桥已有成熟的 MinIO 读通道（receipt/manifest 同模式）；票据与证据同域存放，审计路径一致；单测语义（tests/approval/）只需把 InMemoryTicketStore 换成 MinioTicketStore。
- **缺点**：并发仲裁依赖单写者约定而非存储强制——**多写者场景（M6/M7）必须迁 PG**；mc 管道写入无事务。
- **回退**：删对象即回滚（票据是新增对象，不碰既有数据）。

## 方案 C：本地栈 PG（elemiso-case-pg）

**否决**：该库是案例库域（case_retrieval 专用，独立凭证），混入审批数据破坏域边界。

## 影响

- 拍板 B：M2 存储实现 = MinioTicketStore（约百行）+ 门页只读展示——仍不接真实执行（D-1/D-2/D-3 未拍板），先做存储与只读页。
- 拍板 A：需要新授权项（服务器 schema 变更），建议并入 Controller cutover 授权一起批。
- 两案共享同一 `tools/approval` 语义层，切换成本 = 存储适配层，语义单测不变。
