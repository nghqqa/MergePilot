
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

---

## M8-A2-d 增量合同：verify 完成后的 M4F_RUN（强制）

上述 M8 早期状态机的 review→fix→verify 派发循环之上，追加以下**合同化收口**（M8-A2-d 实测生效）：

| 当前态 | 触发 | 立即动作 |
|---|---|---|
| AWAIT_VERIFY | 房间出现 `TASK_COMPLETED: <run>-verify` 行 + `VERDICT=PASS` 行（两条均为独立消息） | 以**全新独立消息**向同一房间发送 byte-exact 一行：`M4F_RUN: {"contract_version": "1", "run_id": "<run_id>", "trace_id": "<run_id>-tr", "repo": "<repo>", "pr_number": <pr_number>, "test_runner": {"command": "pytest"}, "pr_lifecycle": {"action": "create"}}` |

硬约束：
- **严格顺序**：只观察 review→fix→verify 的完成序列，不跳阶段；M4F_RUN 仅在 verifier 完成且 VERDICT=PASS 后生成一次。
- **字段来源**：run_id / repo / pr_number 逐字取自该 run 的 `TASK_SUBMITTED: {...}` 消息；pr_number 为数字（不加引号）；不编造缺失字段。
- **byte-exact**：该行不得放入 Markdown 代码块、引用/回复前缀或解释；必须是全新独立消息。
- **幂等**：重复的 verifier 完成消息不得产生重复 M4F_RUN（每 run 至多一条）。
- **fail-closed**：VERDICT=FAIL/BLOCKED、字段缺失、解析不确定时**不发送** M4F_RUN。
- **角色边界**：Reviewer/Fixer/Verifier 不得代发 M4F_RUN；Manager 也不得代发 Worker 的 TASK_COMPLETED。
- 这是**受合同约束的状态机响应**，不是自主任务分解。

### 部署配置要求（显式、可审查，非默认）

Manager 需能观察任务房间的普通（非 @mention）群组消息（TASK_SUBMITTED 行与 VERDICT 行），部署时需将 Manager 的 OpenClaw Matrix channel 配置为观察模式（`groups.*.requireMention=false`）并把房间成员加入 `groupAllowFrom`。该配置属于**部署决策**，由部署工具输出显式计划供审查，不作为所有环境的默认值，也不静默热编辑运行容器。

### 已知上游限制（如实记录，非本项目缺陷）

OpenClaw Matrix 通道在外部中断（如 AI 网关重启）后不自动恢复、已消费派单无自动重投递；LLM 对严格单行契约的格式纪律不稳定（可能需要不含逐字 payload 的恢复性提醒）。运行故障的人工恢复步骤（重启 agent 容器等）见部署 runbook，属上游 workaround。
