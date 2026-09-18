# 合并时间线（UTC，2026-08-29）

> 所有事件均为真实运行记录（Matrix 事件 / 容器日志 / 任务存储 meta），证据目录见"证据指针"。

## 阶段 A：PR #1 普通协同闭环（copaw-sandbox，全自主）

| 时刻 | 事件 |
|---|---|
| 03:42:09 | 复赛运行时启动：FileSync mirror_all 拉取 MinIO；Matrix re-login；controller 创建/纳管 worker |
| 03:59 | Leader 委派 sandbox review-1 @reviewer（event `$nadwji…`） |
| 04:01:24 | reviewer 消费（`Created queue`）并开始审查 |
| 04:02:17 | review-1 提交（`TASK_COMPLETED` @manager） |
| 06:17:35 | Leader 委派 fix-1 @fixer |
| 06:20:42 | Leader 委派 verify-1 @verifier |
| 06:21:17 | verify-1 完成（`TASK_COMPLETED: verify-1`）——**PR #1 全链闭环** |

## 阶段 B：PR #2 高危案例 — 首次委派事故（已根因修复，如实披露）

| 时刻 | 事件 |
|---|---|
| 06:25:13–26 | Leader 对高风险项目执行 delegate_task；因**平铺任务命名空间冲突**（与 sandbox 同名 `review-1` 残留 assigned meta），复用分支误判"已委派"，**未发送任何 Matrix 通知**，meta 记录了陈旧 event_id |
| 08:09:06 | 操作员手工重投（@admin DM，无 m.mentions/MXID）——同样不可达（无 mention + 接收端 DM 判定缺陷） |
| 09:5x–16:5x 本地 | **只读审计**定位双根因（taskflow 平铺命名空间复用 + matrix since-token 高水位/DM 误判），见 PHASE14-WINDOWS-COPAW-MATRIX-SYNC-AUDIT-20260829-164840 |

> 说明：该事故成为**工程亮点**——故障被存储级铁证定位（stale event_id、房间历史无事件）、
> 以 12 项单元测试（基线全失败→修复后全通过、全仓零回归）修复，详见 07-disclosures.md。

## 阶段 C：修复版本（build2）重放 — PR #2 高危闭环

| 时刻 | 事件 |
|---|---|
| 09:49:35 | Leader 委派 review-1：event `$IDofCrGE0os3RryOyFkF6R9qdX4cvU7bXL9nj1aiJac`（m.mentions ✓）；reviewer 实时消费 |
| 09:49:37 | reviewer ack（acknowledged_at） |
| ~10:19 | **review-1 结论：FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES**（`TASK_COMPLETED` @manager） |
| ~10:2x | ⛔ **人工安全门**：系统停等，不派发 fix-1/verify-1 |
| ~10:5x | **操作员批准**（确认 HIGH_RISK_FOUND 与 CWE-22；授权 fix-1/verify-1；禁止 merge/push）→ 批准记录落盘 |
| 10:05:5x | fix-1 委派：event `$JU09kIgwjvVJmEUSVSlNR8Va2-zhxsvtmFaZ3RIHuTM` |
| 10:06:24 | fixer 消费 → clone → 修复前探针（200 泄密）→ 最小修复 → 修复后探针（404）→ 交付物齐 |
| ~11:0x | **fix-1 提交**：`STATUS: SUCCESS / FIX_APPLIED / TESTS_PASSED`；Leader 验收 effective=true 并标记计划 fix-1 完成 |
| 11:0x | verify-1 委派：event `$t0dXkuWwmjdkA-Ao6H0Z3KhuYJLJ0nQPCq7w0Jr1aU8` |
| 11:06:25 | verifier 消费 → 独立重 clone → 应用补丁 → 自设计探针 + 回归 |
| ~11:26 | **verify-1 提交**：`STATUS: VERIFICATION_PASSED / SEVERITY: NONE / HUMAN_VERIFICATION_REQUIRED: NO / FIX_INDEPENDENTLY_VERIFIED REGRESSION_CHECK_PASSED` |
| 终态 | **PR #2 保持 OPEN**；未 merge/push/close/reopen |

## 阶段 D：材料与平台（本阶段）

- 双案例合并材料、角色矩阵/DAG/时间线/人工门说明更新、演示脚本 3/8/15 分钟、
  演示平台页面与状态字段设计、秘密扫描与 SHA256SUMS（本目录）

## 证据指针（原始目录，全部只读保留）

| 主题 | 目录 |
|---|---|
| 运行时构建 | PHASE14-WINDOWS-DOCKER-RUNTIME-* |
| 协同闭环/晋升 | PHASE14-WINDOWS-COPAW-SEED-RETRY / -E2E-FULL / -PROMOTION-A-FINAL |
| 凭证轮换 | PHASE14-WINDOWS-CREDENTIAL-ROTATION |
| 高危首次门（被阻断） | PHASE14-WINDOWS-COPAW-HIGH-RISK-HUMAN-GATE-20260829-144234 |
| 同步根因审计 | PHASE14-WINDOWS-COPAW-MATRIX-SYNC-AUDIT-20260829-164840 |
| 修复审计 | PHASE14-WINDOWS-COPAW-HIGH-RISK-FIX-AUDIT-20260829-165813 |
| 修复验证闭环 | PHASE14-WINDOWS-COPAW-HIGH-RISK-FIX-VERIFY-20260829-180548 |
