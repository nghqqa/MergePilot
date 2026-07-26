# M3-A 最终证据 · m3a-final-20260726-04

> **E2E PASS / persistent idempotency verified / crash recovery verified / evidence closed**
> 验证日期:2026-07-26

## run_id

```
m3a-final-20260726-04
```

## 最终 PG 终态

| 表 | 关键字段 | 值 |
|---|---|---|
| task_runs | status / current_stage / verdict | HOLD / verify / FAIL |
| stage_runs | review / fix / verify | COMPLETED / COMPLETED / COMPLETED(verdict=FAIL) |
| dispatch_outbox | review:1 / fix:1 / verify:1 | 全部 DISPATCHED(有 matrix_event_id) |
| stage_events | PROCESSED / DUPLICATE / PARTIAL | 4 / 6 / 2 |
| controller_offsets | sync_token | 持久化 |

## Controller 日志(关键转换)

```
TASK_SUBMITTED m3a-final-20260726-04 → task_run + review PENDING_DISPATCH
outbox #9 → reviewer (DISPATCHED)
reviewer TASK_COMPLETED → fix PENDING_DISPATCH | PG committed
outbox #10 → fixer (DISPATCHED)
fixer TASK_COMPLETED → verify PENDING_DISPATCH | PG committed
outbox #11 → verifier (DISPATCHED)
verify partial snapshot; waiting for VERDICT     ← 流式快照(PARTIAL)
verify partial snapshot; waiting for VERDICT     ← 流式快照(PARTIAL)
verify VERDICT=FAIL → task HOLD | PG committed  ← 完整消息(PROCESSED)
verify 重复事件(已 COMPLETED)→ DUPLICATE           ← 幂等(DUPLICATE ×2)
```

## m3a-verify.sh

```
PASS=12  FAIL=0  WARN=0
✅ M3-A 验收通过(全部关键项)
```

## 崩溃恢复

```
Controller restart → task_runs 3→3 / stage_runs 0→0 / outbox 0→0 / events 70→70
PASS=3  FAIL=0
✅ 崩溃恢复测试通过(Controller 重启后无重复派发)
```

## 6 个阻断问题修复验证

| # | 修复 | 验证 |
|---|---|---|
| ① Verifier VERDICT= 格式 | VERDICT=FAIL 正确解析 | ✅ |
| ② Controller 只解析 VERDICT= | 2 个 PARTIAL 证明不误消费流式快照 | ✅ |
| ③ Verify RUNNING 幂等 | DUPLICATE ×2(已 COMPLETED 后不重复更新 task 终态) | ✅ |
| ④ Submitter 不直接 @reviewer | 只有 outbox #9/#10/#11 派发 | ✅ |
| ⑤ stage_events 回填 run_id/stage | 12 条全有 run_id | ✅ |
| ⑥ m3a-verify.sh 12 项 | 全部 PASS | ✅ |

## 证据文件清单

| 文件 | 内容 |
|---|---|
| db-task-runs.txt | task_runs 完整行 |
| db-stage-runs.txt | stage_runs 完整行(3 阶段) |
| db-stage-events.txt | stage_events 完整行(12 事件 + PARTIAL) |
| db-dispatch-outbox.txt | dispatch_outbox 完整行(3 条 DISPATCHED) |
| db-controller-offsets.txt | controller_offsets(sync_token) |
| controller-full.log | Controller 完整运行日志 |
| matrix-flow.txt | 任务房间完整消息流(49 条,509 行) |
| m3a-verify-output.txt | m3a-verify.sh 12/12 PASS 输出 |
| crash-recovery-output.txt | 崩溃恢复测试 3/3 PASS 输出 |

## 例外样例(保留)

| run_id | 状态 | 用途 |
|---|---|---|
| m3a-final-20260726-02 | HOLD / AGENT_NOT_JOINED | fixer 未 join 房间时 Controller 误标 DISPATCHED 的异常样例 |
| m3a-final-20260726-03 | HOLD / UNKNOWN | verifier 流式快照导致 verdict=UNKNOWN 的异常样例 |

这些异常样例保留了 M3-A 修复过程的证据链,不删除。
