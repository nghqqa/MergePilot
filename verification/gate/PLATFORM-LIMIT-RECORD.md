# REAL_PR_FIX_VERIFY_CANARY — 平台限制记录

## 事实

| 项 | 值 |
|---|---|
| PR | nghqqa/fastapi-boilerplate-demo#16 |
| head | f9500749bcdd10c678e1c4dccd7c9e4fb12745fc |
| 审批尝试 | 2026-09-26T05:20+08:00 |
| 结果 | **HTTP 422 Unprocessable Entity** |
| 原因 | `Review Can not approve your own pull request` |
| 定性 | GitHub 平台安全控制，**不判定为产品缺陷** |
| 处理 | Owner 选择 B：接受平台限制，记录事实 |

## PR 最终状态（只读回归确认）

| 项 | 值 |
|---|---|
| head | f950074（不变） |
| state | OPEN |
| merged | false |
| reviews | 0（审批被平台拒绝） |
| comments | 1（Verifier 链路结果） |
| commits | 仅 fa22506 + f950074（无残留） |

## 只读回归（6/6 PASS）

| # | 检查 | 结果 |
|---|---|---|
| 1 | PR head = f950074 | ✓ |
| 2 | Verifier 4/4 绑定到 f950074 | ✓ |
| 3 | Commit 仅 fa22506 + f950074 | ✓ |
| 4 | speaktype#426 / tizhou#2 零变化 | ✓ PG 4\|0\|2 |
| 5 | 零额外 GitHub 写入 | ✓ |
| 6 | 审批拒绝响应已保存 | ✓（本文档） |

## 后续

- PR 保持 OPEN，未审批，未 merge
- 不使用未经授权的其他账号
- 不执行 merge、close、删除分支
- 如需 merge，须 Owner 在 GitHub 网页上手动操作（使用具有权限的其他账号）
