# tools/dbverify — 数据库迁移验证纳入 PR 验收（决赛 D2）

把“迁移脚本能否安全作用于**历史数据**”作为 PR 验收的一部分，而不只看代码测试。

## 证据等级（如实声明）

| 项 | 本目录的做法 |
|---|---|
| 试验实例 | 从脱敏基线库 `TEMPLATE` 克隆的独立 PostgreSQL 数据库（`trial_kind=ISOLATED_POSTGRES`） |
| 真实性 | **真实 SQL 执行**：迁移在事务中真实运行，失败是 PostgreSQL 真实报错（如 `23502`）；断言真实求值 |
| **不是** | Agentic Database 分支验证。PolarDB 保持 NOT CONNECTED；试验库通过 ≠ 生产发布 |
| 数据 | SYNTHETIC（`case/orders-schema-change/baseline`，确定性生成，无客户数据） |
| 代码版本 | `git mktree` 计算的候选内容树 SHA（真实 git 对象 id，**不是**远端提交） |

## 控制面扩展（`tools/audit-db/m9_migration_verification.sql`）

在不可变的 `revision_bindings` 与既有 `approvals`/`l2_*` 票据之上挂四张**不可变**子表，不新建 run 状态机：

- `data_baselines`：数据基线（schema/data 摘要、行数、PG 版本）
- `migration_candidates`：迁移脚本版本 ↔ revision（`head_sha` 由触发器强制等于 revision 头；同候选修订用 `revision_no` + `parent_candidate_id`）
- `migration_verifications`：验证回写（试验实例身份、环境版本、代码测试裁决、迁移裁决、失败类别、断言、报告摘要；`UNIQUE(candidate, baseline, attempt)`）
- `approval_verification_bindings`：票据 ↔ 验证 1:1，绑定时快照四个摘要

函数：`mv_register_baseline` / `mv_register_candidate` / `mv_record_verification`（幂等重复回调，异摘要拒绝）/ `l2_bind_verification`（审批人绑定，事务内校验，PK 保证并发只有一个成功）/ `db_release_gate(ticket, target_data_digest)`（**每次调用重算** 11 项匹配，结果不落库，旧回调无法使失效结果复活）/ `mv_run_status`。

## 授权执行路径（m9 §5.7）

Policy Gateway 的最终授权执行点是 `l2_claim_ticket()`（APPROVED → EXECUTING 的 CAS）。m9 把闸门接进 claim 本身：
票据若绑定了迁移验证，`db_release_gate(ticket, target_data_digest)` 不 valid → `P0001 DB_RELEASE_GATE_REFUSED`，
**不推进状态、不产生 execution_id、不发生上游写入**；未绑定验证的票据行为不变。网关（`gateway.py`）把
`release_data_digest` 作为验证参数透传（不进 args_hash、不转发上游），把 DB 侧拒绝映射为 DENY `DB_RELEASE_GATE_REFUSED`。
运行器 S12 在真实 PG 上覆盖：stale / 摘要变化 / 未批准 / 过期 / 并发 claim / 重复 claim / 未绑定 / 网关包装映射。
网关模块的真实导入需要 `mcp==1.28.1`（Python ≥3.10）：`conda run -n goai python tools/dbverify/run_migration_loop.py …`；
3.9 下运行器退回 ast 提取并在证据 `environment.gateway_wrapper_mode` 标注。

## 运行

```bash
# 需要一个已灌入 audit-db 全链（含 m9）的 PostgreSQL，例如本任务的临时容器：
python tools/dbverify/run_migration_loop.py --pg-port 55432 --pg-password-file <file> \
    --trial-instance "docker:<container-id>@<image-digest>" \
    --out evidence/FINALS-DB-MIGRATION-LOOP-20260914
```

一次运行完成：基线登记 → run-1 绑定 revision → 代码测试 PASS → **rev1 迁移因历史数据 FAIL（23502）** → 负向（重复/冲突回调、不可变）→ run-2 修订同一候选 → 代码测试 PASS → **rev2 迁移 PASS（11/11 断言）** → 票据 → 并发绑定竞争 → 审批 → 闸门 OK → 目标数据摘要不符即失效 → 追加提交 → 闸门 STALE → 迟到 PASS 回调不能复活 → 生成迁移方案包（`release/migration-plans/...`）→ 证据 + SHA256SUMS → 清理试验库。

`outcome_signature` 是全部步骤/闸门/负向结果的确定性摘要：两次运行相同即闭环可复现（票据 id 等随机值不参与）。

## 测试

- `tests/dbverify/test_m9_contract.py`：SQL 合同 + 迁移链集成（离线）
- `tests/dbverify/test_runner_helpers.py`：摘要镜像、树 SHA、案例夹具（离线）
- `tests/dbverify/test_migration_loop_pg.py`：完整闭环（需 `DBVERIFY_PG_PORT` / `DBVERIFY_PG_PASSWORD_FILE`，否则**跳过**）
