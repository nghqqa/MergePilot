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

## 合并前审查结论（2026-09-15）

| 问题 | 结论 | 落地 |
|---|---|---|
| 迁移票据是否必须绑定验证 | 必须。run 一旦登记过 `migration_candidates`，其票据在 claim 时若无 `approval_verification_bindings` → `MIGRATION_VERIFICATION_REQUIRED` 拒绝；从未登记候选的普通 PR 行为不变（残余边界：登记候选是流水线责任，未登记的 DB 变更只受 `risk_classify` MIGRATION_SCHEMA=L2 人工门约束） | m9 §5.7 + S12 负向 `claim_refused_when_migration_run_unbound` |
| 目标数据摘要的可信来源与缺省 | 来源 = 执行方在**目标库**上用 `tools/dbverify/data_digest.py`（与基线登记同一算法：sorted 表、`COPY … ORDER BY 1`）现算；缺省 NULL **不再**跳过数据比对，绑定票据不带摘要即拒绝 `TARGET_DATA_DIGEST_REQUIRED`（`db_release_gate` 供只读展示时仍可传 NULL） | m9 §5.7 + S12 负向 `claim_refused_without_target_digest_for_bound_ticket` |
| claim → 执行的版本一致性 | **数据侧**：claim 后、`02-migrate.sql` 前再算一次摘要，与 claim 值不一致 → `l2_fail_ticket` 停止。**版本侧（第四轮新增）**：`db_release_gate` 接受 EXECUTING 状态，执行方在迁移前再跑一次闸门——claim 后又推新提交 → `STALE_SUPERSEDED_BY_NEW_REVISION`、票据过期 → `TICKET_EXPIRED`，均停止。保护范围：迁移脚本由 `script_digest` 绑定、不受 claim 后新提交影响；摘要测量真实性属执行方信任边界（下下行） | S12 `recompute_before_migrate_matches_bound_digest` / `data_drift_after_claim_detected` / **`post_claim_gate_recheck_detects_new_revision`** + 方案包 README 步骤 2 |
| 摘要的可信测量与目标绑定（数据库不能验证的部分） | 摘要值由可信执行方（网关/发布器）测量并传入；数据库只能把它与验证基线比对，无法证明该值确实测自目标库。内容级佐证：`01-preflight.sql` 基线画像（137 NULL / 2 重复 / 120 可回填）必须与目标库一致。不为此在 DB 侧引入目标身份参数（不可验证的声明不新增机制） | 方案包 README 步骤 1–2 |
| 函数签名替换、旧重载与权限升级 | m9 `DROP FUNCTION l2_claim_ticket(TEXT,TEXT,TEXT,INTEGER,TEXT)` 后建 6 参签名（第 6 参 DEFAULT NULL，旧 5 参调用方无需改动），自检断言同名函数恰好一个。**第四轮修复**：`tools/m3b-b4-create-roles.sh` 仍按旧签名 REVOKE/GRANT，升级后运行必报 `function does not exist` → 改为 5/6 参双签名 allowlist + `to_regprocedure` 存在性判断，升级前后都可重复运行 | m9 §5.7/§7 + `tests/dbverify/test_m9_upgrade_parity_pg.py`（用旧脚本该测试失败） |
| 新装与升级同一套合同 | 离线 `001-init.sql`（逐字节内含全部 14 个迁移文件）vs「12 个 m9 前文件 + 旧式授权 + 独立 m9」升级路径：m9 面（8 个函数定义/owner/ACL、4 张表结构/约束/触发器）完全一致；`policy_gateway_l2` 在 CLI 路径由 `PREREQUISITE_ROLE_SQL` 预先创建、m9 条件 GRANT 生效，纯 docker-entrypoint 离线 init 不建该角色（与 m9 之前行为一致，由角色脚本事后授权）。行为等价：网关角色可 claim（5/6 参均可解析）、approver 被拒 | `tests/dbverify/test_m9_upgrade_parity_pg.py`（PG-gated 目录快照对等 + SET ROLE 行为断言） |

## 授权执行路径（m9 §5.7）

Policy Gateway 的最终授权执行点是 `l2_claim_ticket()`（APPROVED → EXECUTING 的 CAS）。m9 把闸门接进 claim 本身：
票据若绑定了迁移验证，`db_release_gate(ticket, target_data_digest)` 不 valid → `P0001 DB_RELEASE_GATE_REFUSED`，
**不推进状态、不产生 execution_id、不发生上游写入**；未绑定验证的票据行为不变。网关（`gateway.py`）把
`release_data_digest` 作为验证参数透传（不进 args_hash、不转发上游），把 DB 侧拒绝映射为 DENY `DB_RELEASE_GATE_REFUSED`。
运行器 S12 在真实 PG 上覆盖：stale / 摘要变化 / 未批准 / 过期 / 并发 claim / 重复 claim / 未绑定 / 网关包装映射 / **claim 后新提交（EXECUTING 票据重跑闸门 → STALE）**，共 28 项负向、闸门时间线 10 步。
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
