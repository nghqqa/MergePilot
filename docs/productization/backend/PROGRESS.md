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

## 已完成（本轮二：PostgreSQLTicketStore 实现+真实隔离 PG 验收）

设计基线：`docs/architecture-audit-20260922` 分支 e247b80 的 DATA-ARCHITECTURE-PG.md §5.4（列形状/状态枚举/部分唯一索引/ticket_audit 同事务均已明确，作为实现基线；版本已记录）。

| 工作包 | 内容 | 验证 |
|---|---|---|
| 迁移文件 | tools/approval/pg/migrations/001_approval_tickets.sql（schema approval + tickets + ticket_audit + 夹具父表 run.repos/run.runs[仅满足 FK，正式定义属设计文档]） | 在隔离 PG 应用通过 |
| PostgreSQLTicketStore | tools/approval/pg_store.py：实现 TicketStore 接口；复用 approval.transition 纯逻辑（零状态机重写）；参数化 SQL；显式事务+回滚；SELECT FOR UPDATE + 前置守卫 UPDATE（CAS 可判定）；ticket_audit 同事务；StorageUnavailable 与业务拒绝分离；时间统一 aware UTC（ISO 自动归一）；store.py 再导出（默认路径不切换） | 真实隔离 PG 契约集 7/7 + PG 专属 9/9 |
| PG 专属验收 | NULL(finding_id) 唯一性[NULLS NOT DISTINCT]/非 NULL 唯一/不同目标不互斥/**跨进程竞争（spawn 独立进程）恰好一胜**/终态让位新 attempt/时区与到期边界/回滚原子性（审计与状态同事务）/最小权限（runtime 角色可 DML 不可 DDL）/审计追加核对 | test_store_pg.py 16/16 |
| 迁移工具+演练 | migrate_tickets_sqlite_pg.py：源只读/默认 dry-run/校验（状态/形状/时间/重复活动票）/父记录缺失跳过不伪造/冲突不覆盖可重跑/逐字段核对/事务失败整体回滚 | 测试副本→隔离 PG 演练 5/5（dry-run 不写/导入核对/重跑幂等/非法源中止/孤儿不伪造） |

## 已知偏离与实现期修复（记录，不属设计窗口拍板范围外自作主张）

1. **NULLS NOT DISTINCT（已偏离设计稿并登记）**：设计稿的部分唯一索引未处理 finding_id=NULL（PG 默认 NULL-distinct ⇒ NULL 活动票不受唯一约束）。实现采用 `NULLS NOT DISTINCT`（PG15+，运行环境 PG16 满足）。**待设计窗口确认**；否决则替代=两个部分唯一索引。
2. **实现期真实缺陷修复**（非测试放宽）：
   - `create()` 未把 approval_expires_at 传入票据（到期永为 NULL ⇒ 永不过期）——已修；
   - PG TIMESTAMPTZ 返回 datetime 与调用方 ISO 字符串 now 不可比——transition 入口归一化；
   - `_expired` 源头容错 ISO 字符串/naive UTC（纯逻辑小改，语义不变，SQLite/PG 共同受益）。

## 待设计窗口决定（剩余接口问题）

4. v3_runs / v3_hook_errors 进入统一 PG 的命名空间与保留策略（RunStore PG 适配器的唯一前置）；
5. 预算台账落 PG 表还是仅外部计量（D-4/D-5 决策依赖）；
6. case-pg 的 knowledge 检索数据是否并入统一 PG（pgvector），迁移时机。

## RunStore 后续（未实现，前置=上述#4）

RunStore 契约测试可按 store_contract 同模式抽取；同 delivery 的 legacy/shadow 关联、同 head 重跑与 shadow→on 区分、run/stage/attempt 关系——**契约未明确前不自行实现**（提示词七节）。

## 待外部输入（真实案例线，集中列出）

1. 外部写入与服务变更范围：空提交触发 + check-run 发布 + rag-live/worker 容器操作（CASE1-RUNBOOK §7.1/7.3）；
2. 预算金额与有效限制方式：provider 侧 gateway 上游 key 消费硬上限（本地网关 key 非计费凭证；限频/超时/事后计量均非硬预算）；
3. 凭证处理决定：现网凭证（未轮换）是否满足本次真实运行条件。
