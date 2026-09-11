# Agent 角色矩阵（更新版）

> 覆盖 PR #1（普通协同）与 PR #2（高危安全门）两案例。所有 Agent 为 copaw worker
> 运行时（QwenPaw/CoPaw 栈），经 Matrix(Tuwunel) 通信、MinIO 共享存储交换任务工件、
> Higress 网关统一访问 LLM。

## 角色矩阵

| 角色 | Matrix ID | 职责 | 可用工具/能力 | 权限边界（不可做） | 派发条件 |
|---|---|---|---|---|---|
| **人工操作员**（安全官/裁判） | `@admin` 等（Element Web 登录） | 高危结论的**人工安全门**审批；查看团队房间回放 | Element Web（18088）、只读探测 | 不得替代 Agent 执行任务步骤；不得 push/merge（演示约束） | Reviewer 输出 `HUMAN_VERIFICATION_REQUIRED: YES` 时必经 |
| **Manager / Leader** | `@p14h2-copaw-worker-manager` | 建项目/plan_dag、`ready_nodes`、`delegate_task` 委派、`check_task` 验收、催办、计划状态修复、人工门记录落盘 | projectflow / taskflow / message / filesync；团队房间发言 | **不执行具体修复/验证**；高危未批不得派发后续棒次；不 push/merge | review-1 提交且结论非高危→自主派发；高危→等人工门 |
| **Reviewer** | `@p14h2-copaw-worker-reviewer` | 独立代码审查，输出协议化结论 | taskflow(ack/submit)、filesync、GitHub 只读拉取 | **不修复代码**；不改 PR；无远端凭证 | review-1 委派 |
| **Fixer** | `@p14h2-copaw-worker-fixer` | **最小必要修复** + 本地测试（clone/venv/pytest） | taskflow、filesync、git(本地)、pytest | 只改目标文件；**禁 push/merge/close**；无远端凭证 | 非高危：Leader 自主；高危：人工批准后 |
| **Verifier** | `p14h2-copaw-worker-verifier` | **独立**验证与回归（不信任 Fixer 结论，重 clone 重测） | 同 Fixer | 不得采信未复现的结论；禁 push/merge | fix-1 提交且被 Leader 验收后 |

## 通信与存储协议

- **消息**：Matrix `m.room.message` + `m.mentions.user_ids`（结构化提及）+ 正文首词 MXID
  （可见提及）；事件幂等由 `txn_id`（委派）与 event ledger（接收端）保证
- **任务存储**：MinIO `shared/projects/<project_id>/tasks/<task_id>/{meta.json,spec.md,result.md,workspace/}`
  （project-scoped 布局，杜绝跨项目同名 task_id 冲突；旧平铺路径只读兼容）
- **结果协议**（result.md 顶层标记）：`STATUS:` / `SUMMARY:` / `DELIVERABLES:`；
  审查类任务增加 `SEVERITY:` / `HUMAN_VERIFICATION_REQUIRED:`；验证类任务使用
  `VERIFICATION_PASSED|FAILED`（注意：当前 store 白名单仅接受 SUCCESS 等字面值，
  VERIFICATION_PASSED 属协议扩展，见 07-disclosures.md 第 3 项）

## 状态机（任务生命周期）

```
pending ──delegate_task──▶ assigned ──ack_task──▶ in_progress ──submit_task──▶ submitted
   ▲                          │  (event_id 记录)                                        │
   └──── 计划修复/重派 ────────┘            Leader 验收（effective=true）──▶ completed
```

## 高危门的权限本质

人工门不是"多一个提示"，而是**权限拓扑变化**：门未批时，Leader 对 fix-1/verify-1 的
`delegate_task` 在策略上被禁止（本演示以"不派发"执行），系统处于受控停等；
批准后 Leader 恢复自主派发。PR #1（无门）与 PR #2（有门）共用同一套工具与协议，
体现"安全策略 = 编排拓扑 + 权限策略"的设计。
