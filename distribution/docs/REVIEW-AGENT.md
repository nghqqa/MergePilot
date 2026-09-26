# Review Agent 使用说明

## 概述
Review Agent 是只读的 PR 审查控制台。它从 PostgreSQL 实时读取 skill 回执、
gate 决策和审批票据，推导 PR 当前阶段。

## 页面
| 路由 | 功能 | 数据源 |
|---|---|---|
| /overview | 运营总览（图表+阶段分布+趋势） | /api/overview |
| /pending | 待处理队列 | /api/pending |
| /repos | 仓库列表 | /api/pulls |
| /repos/:o/:n/pr/:n | PR 详情（钻取） | /api/pulls/:n?repo= |
| /core | 系统状态与接线 | /api/audit + 五面 |

## 阶段推导规则（后端权威）
| 阶段 | 条件 | stage_source |
|---|---|---|
| ACTION_REQUIRED | 存在未过期 PENDING 票据 | approval.tickets |
| BLOCKED | gate REFUSE 或回执 integrity≠OK | skill_gate_audit / receipts |
| PASSED | 回执齐备 + gate PRODUCE + 无待办 | skill_gate_audit |
| REVIEWING | 有回执、无 gate 决策 | skill_receipt_outbox |
| STALE | 同 PR 存在更新 head | head-ordering |
| REMEDIATING/VERIFYING | 需要 Fixer/Verifier（禁用）| 恒 0，不虚构 |

## 使用边界
- **只读**：不执行任何 GitHub 写入
- **不替代人工审查**：阶段仅供参考
- **BLOCKED ≠ PASSED**：阶段永不改写
