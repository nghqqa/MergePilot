# M3-B4c.1 证据 · 收敛与调度加固(确定性拒绝 / 公平调度 / 工作预算 / Gateway 降级)

> **Controller L2 drain 收敛分类 + 公平/预算调度 + Gateway circuit breaker;不动 m3b-b4c-closed/旧 migration/旧 evidence。**
> 验证日期:2026-07-28 | 标签:`m3b-b4c.1-closed`(SHA 见 [标签SHA映射](../../docs/标签SHA映射.md))
> 关联代码:`tools/audit-db/m3b_b4c1.sql` + `tools/workflow-controller/{controller,gateway_client}.py` + `tools/m3b-b4c1-hardening.sh`

## 不变量(未破坏)

PostgreSQL 唯一权威;Gateway 调用不持 DB 事务;UNKNOWN/EXECUTING 绝不重派;merge/close 经票据 claim + canonical payload + TOCTOU;多 Controller 下仅一个 binding/ticket/有效 claim。

## 落地(B4c.1-0/1/2,4 个提交)

- **`m3b_b4c1.sql`(独立 migration,幂等)** — task_runs 调度字段(`l2_next_attempt_at`/`l2_retry_count`/`l2_retry_reason`/`l2_discovery_deadline_at`)+ 非负 CHECK + ready 部分索引;outbox.last_error_code;`l2_reject_approved(ticket, reason)`(allowlist reason,仅 APPROVED+未 claim+未过期,未知 reason→22023,owner `mergepilot_l2_owner`,仅 GRANT mergepilot)。
- **gateway_client.py typed 异常** — `GatewayDenied`/`GatewayUnavailable`/`GatewayGlobalDegraded`(皆 GatewayError 子类)+ `_classify_error_text` 解析 `reason_code`。
- **controller.py** — `GatewayOutcome`(SUCCESS/TRANSIENT/TICKET_DENY/GLOBAL_DEGRADED);`_advance_outbox_by_approval(outcome)`(approval 权威,仅仍 APPROVED 才按 outcome 分类);drain 补 `expires_at>now`+`next_retry_at<=now`,`attempts` 仅真实派发 +1;
  **circuit breaker**(_L2_GW:TRANSIENT/GLOBAL_DEGRADED→打开,degraded_until 过后自动恢复,SUCCESS→关);
  **公平调度**(候选 `l2_next_attempt_at<=now` + 公平序;**发现期限** `l2_discovery_deadline_at` 代替旧 `L2_DISCOVERY_MAX` 计数 HOLD + 退避;`pg_try_advisory_lock` 未取锁跳过);
  **工作预算**(每 tick 共享 `deadline=monotonic()+budget`,传 initiate/drain/reconcile;单次 GW 超时 ≤ 剩余预算);
  **Gateway 降级启动**(TCP 不可达→DEGRADED_NETWORK 不 fatal,纯 DB 收敛继续,恢复后自动续;缺 token/migration 仍 fatal);删 `L2_RECONCILE_AGE` env→常量 120。

## 验证(B4c.1-3,26/26 PASS · fixture 隔离)

`tools/m3b-b4c1-hardening.sh`:source `e2e-lib.sh` + `e2e_guard`,经测试 Gateway `policy-gw-e2e` 在 fixture 仓建真实 PR/票。

| # | 场景 | 关键断言 |
|---|---|---|
| 1 | migration/ACL(6) | 幂等连跑;ready 索引;`l2_reject_approved` owner=mergepilot_l2_owner;mergepilot 可 EXECUTE;approver 不可;未知 reason→22023(allowlist) |
| 2 | 确定性拒绝(5) | CLAIM_MISMATCH→approval FAILED/outbox FAILED/task HOLD(l2_drain_denied)/attempts=1;**强制 lease 过期再 drain:attempts 仍 1**(无手工中和) |
| 3 | 瞬时退避(6) | 不可达 Gateway→approval 留 APPROVED/outbox DISPATCHED/attempts=1/next_retry_at 未来/last_error_code=TRANSIENT;立即再 drain attempts 不长 |
| 4 | 公平调度(2) | outbox `next_retry_at` 未来→不领取(attempts=0);到期→领取并 MERGED |
| 5 | 工作预算(2) | 单 tick 处理 ≤ MAX_ITEMS=3(未一次处理 5);下一 tick 处理剩余(累计 5) |
| 6 | fixture 回归(4) | discover+建票→l2_awaiting_approval;**B4d approve CLI**→APPROVED(session_user);drain→MERGED(正向全链) |
| 7 | 凭证(1) | 输出无 PGPASSWORD/PASS/token |

结尾门:`[ PASS -eq 26 ] && [ FAIL -eq 0 ]`(防"少跑仍绿")。

### 证据文件
- `hardening-test.out`:26/26,含各场景原始输出 + DB 断言。
- `db-snapshot.txt`:本测试 run 终态快照。
- `credential-scan.txt`:无泄漏结论。

## B4c.1.1 修正(复审 P1×7 + 负向矩阵,16/16;tag `m3b-b4c.1.1-closed`)

B4c.1 复审发现 7 阻断缺口 + 证据缺口,B4c.1.1 修复(`m3b_b4c1_1.sql` + controller/gateway_client/start 脚本):

| # | 问题 | 修复 |
|---|---|---|
| 1 | RETRY 不重新排队(占满 LIMIT 饿死) | `_atomic_advance`/`create_ticket_for_run` 的 RETRY 路径经 `_l2_requeue` 重排(retry_count++/next_attempt_at=now+backoff) |
| 2 | reconcile 无公平性/无读失败退避 | reconcile_items 查询加 `next_retry_at<=now`+ORDER+LIMIT MAX_ITEMS;`_reconcile_ticket` 读失败经 `_l2_outbox_backoff` 持久退避 |
| 3 | breaker 只覆盖 drain;helpers 吞 GlobalDegraded | gateway_list/read helpers 改 `except (Unavailable,Denied)`,**透传 GatewayGlobalDegraded**;discover 包 catcher(开 breaker);initiate/reconcile 顶 + 循环检 `_l2_gw_degraded` |
| 4 | 预算非硬边界(LIMIT 10,单调用超预算) | initiate/reconcile LIMIT MAX_ITEMS;`_gw_timeout_for` 单调用 ≤ 剩余;`gateway_list_prs` 分页共享 deadline |
| 5 | deny 无完整 CAS | TICKET_DENY 先 `l2_reject_approved`(查返回;FALSE→回滚不终结)+ 锁 task FOR UPDATE + CAS rowcount + 条件 outbox |
| 6 | reason 正则 `[A-Z_]+` 截断数字码 → "L" | 改 `[A-Z0-9_]+` |
| 7 | 预检不要求 B4c.1 migration | startup_assert_l2 + start-controller-container.sh 检 `l2_reject_approved` + 调度列(EXISTS+CASE 防函数缺失 ERROR);lease<timeout+5 → **FATAL**(默认 lease 90);`l2_reject_approved(NULL)` 显式拒 |

`m3b-b4c1_1-hardening.sh` **16/16**:migration/ACL+NULL 拒、deny 回归+#5 并发 CAS(claim 后 deny 不覆盖)、#1 RETRY 重排、#3/#4 drain breaker(首条 TRANSIENT 停本 tick)、#7 预检(lease FATAL/缺 migration FATAL/正常通过)、#4 预算下限(≥1≤MAX_ITEMS)、凭证 clean。结尾 `[PASS-eq 16]&&[FAIL-eq 0]`。

### B4c.1.1 证据文件
- `b4c11-test.out`:16/16 原始输出 + DB 断言。
- `credential-scan-b4c11.txt`:无泄漏结论。

## 后续
- **B4e**:review→fix→verify→approve→drain→merge 总 E2E + 崩溃恢复录像。
- **B5**:负向证据 8 项。
