# WH 轮(webhook 自动链路 · 全工具链)三案例 · 2026-09-19

PR #1/#2/#3 经 `pull_request synchronize` webhook 自动触发重跑,三路径全覆盖,本轮确定性 Skill 全链激活:

| 案例 | run | check | 结论 |
|---|---|---|---|
| PR #2 | run-gh-pr2-e9731abd-064416 | success | HIGH → human gate APPROVED → fix → VERIFIED (full toolchain) |
| PR #3 | run-gh-pr3-3daa6fb4-065708 | failure | HIGH finding — human gate REJECTED (blocked, zero dispatch; full toolchain) |
| PR #1 | run-gh-pr1-0fae3afd-070752 | success | passed (auto-completed, low-risk path; full toolchain) |

链路:GitHub webhook → 验签(159.75.42.106)→ gh-bridge → AgentTeams 真实执行(Reviewer/Fixer/Verifier
+ 确定性 Skill 真实调用,见各包 skill-audit.json)→ result.md 权威判读 → App check-run 回写 PR。
门决策(PR2 批准/PR3 拒绝)为操作员经 MinIO 记录+Matrix 指令;系统全程零 GitHub 写入(除 check run),
不自动 merge——合并决策归维护者。

补丁确定性:PR #2 修复补丁 sha256 `674356fc…16081` 再次逐字节一致(历史 9 次后第 10 次独立产出)。
历史证据(R1→SK5 九轮+首次 WH 轮)全部保留不动。
