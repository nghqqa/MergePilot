# M3-B4c 证据 · Controller 侧 L2 审批闭环(发现 → 建票 → drain → 对账 + 并发/崩溃/E2E)

> **Controller 持有唯一任务状态;L2 动作经 Coordinator token 调 Gateway,绝不自动重 merge。**
> 验证日期:2026-07-27 | 标签:`m3b-b4c-closed`(SHA 见 [标签SHA映射](../../docs/标签SHA映射.md))
> 关联设计:[M3-B4-审批票据与Action-Outbox设计](../../docs/M3-B4-审批票据与Action-Outbox设计.md)
> 关联代码:`tools/workflow-controller/controller.py` + `tools/audit-db/m3b_b4c.sql`

## 状态机(Controller 侧,PG 权威)

```
verify PASS + approval_required=TRUE
  → task=APPROVAL_PENDING / current_stage=l2_binding
  → initiate_l2_pending(): discover_binding_for_run(B4c-1) → create_ticket_for_run(B4c-2)
  → l2_approve → approval=APPROVED
  → drain_l2_outbox(B4c-3): outbox DISPATCHED+lease → Gateway merge → 读 approvals 权威态推进
  → reconcile_l2(B4c-4): UNKNOWN/超时EXECUTING/滞留DISPATCHED/过期 收敛(绝不重 merge)
```

## 关键不变量(评审主线)

1. **绝不自动重新 merge**:drain 候选只认 `PENDING_DISPATCH+APPROVED`(或 lease 过期的 `DISPATCHED+APPROVED`);`UNKNOWN/EXECUTING/APPROVED` 不重派。对账收敛后复用 `_advance_outbox_by_approval`。**恰好一次**以审计行 `mcp_calls.reason_code='L2_CLAIMED'` 计数为准(B4c-5 第 3/7 项实测:收敛前后计数不变 = 1)。
2. **并发互斥**:per-run session advisory lock(`disc:<run>` / `ticket:<run>`)序列化发现+建票;drain 用 `SELECT FOR UPDATE SKIP LOCKED LIMIT 1`;`uq_run_pr_bindings_run` + `uq_active_ticket_per_binding_action` 双唯一索引兜底。B4c-5 第 7 项实测:双 Controller 容器并发下 1 binding / 1 ticket / attempts=1 / 1 次 L2_CLAIMED。
3. **故障域分离**:L2 域(PG-驱动)始终运行,Matrix 不可达不阻断 L2 恢复。
4. **fail-closed 启动**:`startup_assert_l2` 对非法 `L2_MERGE_ENABLED` / 缺 `COORDINATOR_TOKEN` / Gateway 不可达 / l2 函数不可 EXECUTE → 拒启动。
5. **审计不可变**:`mcp_calls` 受 `mcp_calls_immutable()` 触发器约束为 INSERT-only(superuser 的 `DELETE` 亦被拒,B4c-5 复测确认)。

## 验证(B4c-0..5 累计全 PASS)

| 阶段 | 证据目录 | 结果 |
|---|---|---|
| B4c-0 migration | `0-migration/migration-test.out` | **38/38**(l2_ensure_ticket 双 TTL 比对 + 未终结票据唯一索引原子迁移) |
| B4c-0 controller | `0-controller/controller-test.out` | **19/19**(fail-closed 启动 / STARTUP_CHECK_ONLY 部署预检门 / 故障注入恢复**原容器同 ID** / 凭证 clean) |
| B4c-1 绑定发现 | `1-discover/discover-test.out` + `schema-unit.out` | live **32/32** + schema 单元 **36/36**(GitHub 权威读回 + branch 双源 SHA + 原子 CAS) |
| B4c-2 幂等建票 | `2-ticket/ticket-test.out` + `pgwait-unit.out` | **31/31** + PG-wait 单元 **7/7**(全锁事务 + args_hash 契约 + 22023 分类) |
| B4c-3 lease drain | `3-drain/drain-test.out` | **31/31**(三边界 + attempts 准确 + action-aware + 对称 CAS) |
| B4c-4 延迟对账 | `4-reconcile/reconcile-test.out` | **20/20**(UNKNOWN/滞留/过期收敛 + 对称 CAS) |
| **B4c-5 闭环** | `5-e2e/e2e-test.out` | **42/42**(下表) |

### B4c-5 闭环验收(42/42)

| # | 场景 | 关键断言 |
|---|---|---|
| 1 | 全链 E2E(`l2_binding`→MERGED,经 `initiate_l2_pending` 主循环入口) | task MERGED + `result_sha` 固化 + 恰好 1 次 L2_CLAIMED |
| 2 | 超时 EXECUTING 对账(未合并→FAILED / 已合并→USED) | `l2_reconcile_executing` 分支(B4c-4 只测 UNKNOWN) |
| 3 | UNKNOWN 对账 + **绝不重新 merge** | L2_CLAIMED 计数收敛前后不变=1;第二张票被 `uq_active_ticket_per_binding_action` 拒 |
| 4 | Controller 级 DENY(exec-TTL 过期) | EXPIRED→HOLD,0 次 L2_CLAIMED |
| 5 | Gateway CLAIM_MISMATCH 异常 | 票仍 APPROVED,不 claim / 不 merge |
| 6 | DISPATCHED lease 后崩溃 → **真容器 restart 恢复** | attempts 1→2,滞留 DISPATCHED→MERGED,恰好 1 次 L2_CLAIMED |
| 7 | **双 Controller 并发**(两容器 run_forever) | 1 binding / 1 ticket / attempts=1 / 1 次 L2_CLAIMED |

### B4c-5 审计证据(`5-e2e/`)

- `e2e-test.out`:42/42,含每个 run 的 approval/outbox/task/stage/result_sha/claims 内联断言(**权威**)。
- `db-snapshot.txt`:8 个 run 的终态(`run_id | task | stage | approval | outbox | attempts | result_sha`)。
- `mcp-calls.txt`:L2 审计行(per-ticket 的 `L2_CLAIMED`/`L2_COMPLETE`/`CLAIM_MISMATCH`)。注:审计跨测试运行持久累积(INSERT-only 触发器拒 DELETE),per-ticket L2_CLAIMED 计数以内联断言为准。
- `controller-A-logs.txt` / `gateway-logs.txt`:容器日志尾段(崩溃恢复 + 并发 tick 可见)。
- `github-residue.txt`:本测试在 `nghqqa/MergePilot` 创建/合并的真实 PR 清单(run_id → 状态)。
- `credential-scan.txt`:无凭证泄漏。
- `run-py-raw.log`:controller 一次性调用原始输出(含 `[ctrl]` 日志,排障用)。

## 后续(不属 B4c,留待后续里程碑)

- **B4d**:approve CLI(当前 approve 经 `l2_approve` SQL,无命令行入口)。
- **B4e**:总 E2E(含 review→fix→verify 全 Agent 链 + 崩溃恢复录像)。
- **B5**:负向证据 8 项(直连拒 / list 过滤 / 跨角色拒 / fixer 写约束 / 票据伪造-过期-重复拒 / 合法票据只成功一次 / 全审计)。
