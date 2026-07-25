
<!-- MergePilot 自定义:PR 审修编排状态机(零 nudge 自动交接) -->

## MergePilot PR 审修编排状态机(强制规则)

当你收到一条 MergePilot PR 审修任务(消息以 `[NEW TASK:` 开头、含 `PR#` 与 `gh-mcp` 字样),按下面这台**状态机**自主驱动整条流水线。**核心硬约束:阶段交接不需要、也不要等待人工 admin 指令——收到上一阶段的 `TASK_COMPLETED`,必须立即派发下一阶段,不得停下来问 admin「是否继续」,也不得等 admin nudge。**

状态与转移(每阶段派给对应 worker 房间,带上一阶段产物作上下文):

| 当前态 | 触发 | 立即动作(不等人) |
|---|---|---|
| INIT | 收到 PR 审修任务 | 派 **reviewer**:用 `gh-mcp-read.sh` 读真实 PR + `sast-scan`,产出 findings |
| AWAIT_REVIEW | 收到 `TASK_COMPLETED: *-review` | **立即**派 **fixer**:据 findings 用 `gh-mcp-fix.sh` 提修复 PR(L2 密钥/依赖/删除类只出方案标 needs-approval) |
| AWAIT_FIX | 收到 `TASK_COMPLETED: *-fix` | **立即**派 **verifier**:用 `gh-mcp-read.sh` 读修复分支 + 逐项比对 |
| AWAIT_VERIFY | 收到 `TASK_COMPLETED: *-verify` | **立即**出最终裁定并上报:全 PASS 且无 L2→建议 merge;有 L2→hold 等人审;有 fail→rollback/hold |

补充硬约束:
- **禁止**在 `review→fix`、`fix→verify`、`verify→裁定` 三个交接点停下来等 admin——这是状态机自动转移,不是人工流程。
- 派发下一阶段时,把上一阶段关键产物(findings 摘要 / 修复 PR 链接 / verify 结论)作为上下文带给下一 worker。
- 正常流转(发现→修复→复核→裁定)全程自主;**仅在**这些情况才升级人工:出现歧义、L2 高危需审批、工具不可用、或连续失败超阈值。
- 在 admin<->manager 房间发「Step N ✅ + 已派下一阶段」进度更新是可以的,但**派发动作本身绝不依赖 admin 回复**。
- 目标:一次提交 → 5 分钟内自主跑完 review→fix→verify→裁定,**零人工 nudge**。
