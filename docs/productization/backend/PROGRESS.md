# 后端执行进度（BACKEND PROGRESS）

**分支**：`feat/backend-pg-storage`（基于 `chore/backfill-r3-ops` @ `67d6446`）｜ **负责窗口**：后端实现
**文件责任边界**：本窗口拥有 `tools/approval/store*`、`tools/integration_prep/`、`tools/costmeter/hooks.py`、`tests/approval/`、`tests/integration_prep/`、`docs/productization/backend/`；**不修改** ARCHITECTURE-V3（设计窗口）与 tools/console_v3（管理平台窗口）。

## 基线与状态

- 基线提交：`c179601`（受控案例准备完成：repo 约束缺失为其中已知项，本轮修复）
- 本轮分支提交：见 git log（执行保护修复 → PG 准备 → 盘点工具）

## 已完成（本轮）

| 工作包 | 内容 | 验证 |
|---|---|---|
| 执行保护① 定向认领补 repo | 过滤三元组 = repo+PR+完整 head（`MERGEPILOT_TARGET_REPO/PR/HEAD`，三全才激活，格式非法拒绝） | 回归测试更新+新增（缺一即不激活/非法拒绝/SQL 谓词断言） |
| 执行保护② 发布五分类 | reporter 脚本结构化 HTTP 错误；outcome ∈ {unknown, auth(401), retryable(429/5xx/403限频), permanent(403非限频/404/422)}；unknown → PUBLISH_UNKNOWN(manual-reconcile) 停止自动重试 | 7 项分类测试 |
| 执行保护③ 取消范围 | 纯逻辑计划（先 leader 防再委托 → reviewer/fixer/verifier → 静默核验 → 在途计费提示 → 恢复注记）+ 前置校验（独占+无其他活动项目）+ 取消后核验 | 5 项纯逻辑测试（容器操作本轮未执行） |
| 隔离 PG 环境 | 容器 `mp-pg-contract-test`（postgres:16，127.0.0.1:55432，db=mp_contract，独立卷）；psycopg2 2.9.12 可达 | 连接+版本实测 |
| PG 可行性 spike | 部分唯一索引（活动票唯一）+ 前置守卫 UPDATE CAS + 终态让位新 attempt——在真实 PG 16 上 3/3 通过 | tests/approval/test_pg_cas_spike.py |
| 契约测试抽取 | tests/approval/store_contract.py：7 项契约（创建往返/幂等创建/CAS/跨连接竞争/并发收敛/崩溃恢复/红线往返）——实现无关，PG 适配器落地后直接复用 | SQLite 上 50 passed |
| 迁移源盘点工具 | migration_inventory.py（PRAGMA 只读盘点表/列/DDL/行数 + 兼容性注记） | 双库演练：tickets(18列)/v3_runs(22列)；注记=expires_at/approved_at 无类型声明列迁 timestamptz 需格式校验 |

## 版本一致性（repo vs r3work 运行副本，2026-09-22 实测）

- `gh_bridge.py`：**不同步**——repo 侧含本轮执行保护修复（repo `e68db380…` vs 运行副本 `840b193c…`，R4 时的版本）。
- orchestrator 10 文件：全部一致。
- 结论：真实案例（待授权）前需要一次**增量同步**（仅 gh_bridge.py），方案同 R4（备份→校验→复制→off 冒烟），**待新增授权执行**。
- 明细：backend/version-delta-20260922.json

## 待设计窗口决定（接口问题，非竞争方案）

1. tickets 迁移 PG 的正式列清单与类型（尤其 approval_expires_at/approved_at：timestamptz vs 保留 ISO 字符串）；
2. 活动票唯一是否采用 PG 部分唯一索引（spike 已证明可行性，见 test_pg_cas_spike）；
3. ticket_id 形态：保留 `tkt-` 前缀 TEXT PK 或改 UUID；
4. v3_runs / v3_hook_errors 进入统一 PG 的命名空间与保留策略；
5. 预算台账落 PG 表还是仅外部计量（D-4/D-5 决策依赖）；
6. case-pg 的 knowledge 检索数据是否并入统一 PG（pgvector），迁移时机。

## PostgreSQLTicketStore 实现前置条件

- 设计窗口契约确认上述 1–3（表结构级）后即可实现：语义层直接复用 approval.transition（零改动），PG 侧仅需 `_Txn`（BEGIN IMMEDIATE → `SELECT ... FOR UPDATE`/事务隔离映射）与 DSN 连接；契约集（store_contract.py）原样复用即为验收。
- 环境已就绪（隔离容器 55432），无外部阻塞。

## 待外部输入（真实案例线，集中列出）

1. 外部写入与服务变更范围：空提交触发 + check-run 发布 + rag-live/worker 容器操作（CASE1-RUNBOOK §7.1/7.3）；
2. 预算金额与有效限制方式：provider 侧 gateway 上游 key 消费硬上限（本地网关 key 非计费凭证；限频/超时/事后计量均非硬预算）；
3. 凭证处理决定：现网凭证（未轮换）是否满足本次真实运行条件。
