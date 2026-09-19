# WH 轮(webhook 自动链路)三案例 · 2026-09-19

PR #1/#2/#3 经 `pull_request synchronize` webhook 事件自动触发重跑,三路径全覆盖:

| 案例 | run | check | 结论 |
|---|---|---|---|
| PR #2 | run-gh-pr2-1414edbe-052650 | success | HIGH → human gate APPROVED → fix → VERIFIED |
| PR #3 | run-gh-pr3-9fd86556-053436 | failure | HIGH finding — human gate REJECTED (blocked, zero dispatch) |
| PR #1 | run-gh-pr1-fa3f85f8-054038 | success | passed (auto-completed, low-risk path) |

链路:GitHub webhook → 验签入库(159.75.42.106)→ gh-bridge(播种/唤醒/kickoff)→ AgentTeams 真实执行 → result.md 权威判读 → App 身份 check-run 回写 PR。
PR #2 门批准/PR #3 门拒绝为操作员决策(MinIO 记录+Matrix 指令),修复与验证仍全 Agent 执行。
历史九轮证据不动;本包为增量新证据。
