# E2E 证据包 · PR#6 零-nudge 隔离审修(iso5-pr6,2026-07-25)

> 一次提交,reviewer→fixer→verifier 在**专属任务房间**内,由 **v2 watcher 真 @mention** 驱动,全程**零人工 nudge + 零跨-PR 污染**。

## 场景

- 仓库:`nghqqa/mergepilot-test`,PR#6(分支 `feature/m1-e2e` → `main`)
- 改动文件:`user_service.py`(SQLi + 硬编码密钥 + 连接泄漏 + 缺校验/错误处理)
- 任务房间:`!61wZDTSDQDAkyKeTmM`(per-task room,5 成员:admin+manager+reviewer+fixer+verifier)
- 任务前缀:`iso5-pr6`

## 端到端流程(经 v2 watcher 真 @mention 驱动)

| 阶段 | 触发者 | 动作 | 产物 |
|---|---|---|---|
| admin 提交 | admin | `@reviewer` 真 mention 任务(PR#6 审查指令) | matrix-flow.txt L1-5 |
| reviewer 审查 | reviewer | gh-mcp-read.sh 读 feature/m1-e2e → sast-scan → 6 findings → TASK_COMPLETED: iso5-pr6-review | findings.md(107行) |
| **watcher review→fix** | v2 watcher(admin) | 检测 TASK_COMPLETED → `@fixer` 真 mention(据 findings 提修复 PR) | matrix-flow.txt L60-70 |
| fixer 修复 | fixer | L2(密钥)只出方案;L0/L1 创建唯一分支 `fix/iso5-l0l1` + 写修复 + TASK_COMPLETED: iso5-pr6-fix | l2-plans.md(113行)+ plan.md |
| **watcher fix→verify** | v2 watcher(admin) | 检测 TASK_COMPLETED → `@verifier` 真 mention(复核修复分支) | matrix-flow.txt |
| verifier 复核 | verifier | 逐项比对(F2-F6 ✅ Pass;F1 密钥需人工吊销) → Verdict: **blocked-needs-approval**(L2 HOLD) → TASK_COMPLETED: iso5-pr6-verify | matrix-flow.txt 尾部 |

## 关键验证点

1. **零人工 nudge**:review→fix→verify 三次阶段交接,全由 v2 watcher 自动 @mention 驱动,无人工干预。
2. **任务隔离**:全程在单一任务房间 `!61wZDTSDQDAkyKeTmM` 内,OpenClaw 按房间隔离 session → 零跨-PR 污染。
3. **worker 只认真 @mention**:admin 发的是 `formatted_body + m.mentions` 的真 mention 胶囊(plain text 无效)。
4. **唯一修复分支**:fixer 用 `fix/iso5-l0l1`(任务前缀命名),不复用旧分支名。
5. **L2 策略正确**:密钥类 finding → fixer 只出方案(plan.md/l2-plans.md)不自动修;verifier 裁定 blocked-needs-approval。

## 证据文件

| 文件 | 内容 |
|---|---|
| `matrix-flow.txt` | 任务房间完整消息流(48 条,418 行,不截断)——含 admin @reviewer / watcher @fixer / watcher @verifier / 各 worker TASK_COMPLETED + verifier 逐项裁定 |
| `tasks/iso5-pr6-review/findings.md` | reviewer 的 6 条 findings(sast-scan 实测 + 人工补充) |
| `tasks/iso5-pr6-fix/l2-plans.md` | fixer 的 L2 修复方案(密钥类只出方案) |
| `tasks/iso5-pr6-fix/plan.md` | fixer 的修复计划 |
| `watcher-v2.log` | v2 watcher 日志(**注**:仅含 dedup 修复后的重启日志;iso5 运行时的 watcher 驱动证据保留在 matrix-flow.txt 的 @mention 消息中——每条 `@fixer`/`@verifier` 即 watcher 发出的阶段推进) |

## 诚实备注

- 本运行(iso5)**先于 watcher 幂等去重修复**:matrix-flow 中 reviewer 重复发 TASK_COMPLETED(3x)→ watcher 对应发 3 次 @fixer(功能无碍,已在本轮 Section 8 item 2 修复)。
- watcher-v2.log 在 dedup 重启时被覆盖;watcher 驱动的直接证据是 matrix-flow.txt 里的 `@fixer`/`@verifier` mention 消息(发送者=admin=watcher 身份)。
- 无单独 verify-report.md(verifier 将裁定直接写入房间消息流,matrix-flow.txt 尾部)。

## GitHub 关联

- 审查目标:https://github.com/nghqqa/mergepilot-test/pull/6
- 修复 PR(fixer 创建):分支 `fix/iso5-l0l1` → `feature/m1-e2e`
