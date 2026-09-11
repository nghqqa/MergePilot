# 人工安全门（Human Security Gate）设计与真实门记录

## 设计原理

高危漏洞的"修复授权"不应由任何 Agent 自行决定。系统将人工审批建模为 DAG 中的
**受控停等节点**：

1. Reviewer 输出协议化结论，其中 `HUMAN_VERIFICATION_REQUIRED: YES`
   （等价语义：HIGH_RISK_FOUND 高危确认）是**唯一**能触发人工门的信号；
2. 门未批期间：Leader **不派发** fix-1/verify-1（策略禁止），系统停等并保留全部证据；
3. 批准以**落盘记录**为准（`shared/projects/<pid>/human-gate-approval.md`，含审批范围与禁令），
   随任务存储同步，可审计、可重放；
4. 批准后 Leader 恢复自主派发；拒绝/超时则停止并保留证据（fail-safe）。

## 本次真实门记录（2026-08-29）

- 触发：review-1 结论 `FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES`
  （CWE-22 路径穿越任意文件读取，`demo_download`）
- 审批内容（操作员确认）：
  1. HIGH_RISK_FOUND 结论确认；
  2. 漏洞定性 CWE-22 确认；
  3. 授权 Leader 派发 fix-1；
  4. 授权 Fixer 最小必要修复并运行测试；
  5. 授权 fix-1 完成后 Leader 自主派发 verify-1；
  6. 授权 Verifier 独立验证与回归；
  7. **禁止自动 merge/push/close/reopen**；
  8. **PR #2 保持 OPEN**；
  9. 不修改 PR #1 / wd1-pr1-bootstrap / copaw-sandbox；
  10. 不触碰 OpenClaw 主线与 WSL；
  11. 不输出/落盘任何 password、token、API key、Cookie；
  12. 修复或验证失败即停止并保留证据。
- 落盘：`artifacts/human-gate-approval.md`（与共享存储内记录一致）
- 后果链：批准 → fix-1 委派（event `$JU09kIgw…`）→ Fixer 提交（SUCCESS/TESTS_PASSED）
  → verify-1 委派（event `$t0dXkuWw…`）→ Verifier 独立通过（SEVERITY: NONE）

## 面试/评委常见问题

- **门是谁实现的？** 不是外挂脚本：触发条件来自 Reviewer 的协议化输出，门效应体现在
  编排策略（Leader 不派发）与存储记录（批准文件），与任务/消息层同一套基础设施。
- **为什么 PR #2 保持 OPEN？** 演示边界：系统被授权完成"发现→审批→修复→验证"，
  合并属仓库维护者的人工决策。任何自动 merge 都超出授权且违背 fail-safe 原则。
- **如果操作员拒绝会怎样？** 按设计进入 `STOP` 分支：保留审查与证据，任务标记失败原因，
  不产生任何修复或推送（本次演示未触发该分支）。
