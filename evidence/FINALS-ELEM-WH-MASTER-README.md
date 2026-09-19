# WH 收官轮(全工具链)三案例 · 2026-09-19

PR #1/#2/#3 经 webhook 自动触发,**四链路齐备**:真实 GitHub webhook(HMAC 验签)+
确定性 Skill 真实调用 + RAG 组织规范检索(citation-only)+ AgentLoop OTel span 直连上报。

| 案例 | run | check | 结论 |
|---|---|---|---|
| PR #2 | run-gh-pr2-65de83d6-085056 | success | HIGH → human gate APPROVED → fix → VERIFIED (skill+rag+otel 全链) |
| PR #3 | run-gh-pr3-03312d65-091817 | failure | HIGH finding — human gate REJECTED (blocked, zero dispatch; rag 命中 CWE-78 组织规范) |
| PR #1 | run-gh-pr1-575aa8e1-093022 | success | passed (auto-completed, low-risk path; skill 真实调用) |

- 补丁确定性:PR #2 修复补丁 sha256 `674356fc…16081` **第 11 次**独立产出一致,Verifier 全新 clone 独立复现同哈希。
- RAG 纪律:PR #3 Reviewer 确认 CWE-78 后检索命中 `org-standards/cwe-78-command-injection.md`(references only,
  不替代自主 PoC 复现);PR #2 轮 rag_mcp 注入有日志实证(08:51:05),同会话前一轮(d1c2f630)rag 真实调用命中 cwe-22 规范。
- 不自动 merge:App 仅 Checks 读写权限;三 PR 全程 OPEN;门决策为操作员投递(MinIO 记录+Matrix 指令)。
- AgentLoop:会话累计 1528 span(四 worker),OTEL_EXPORT 全部 SUCCESS;控制台按 service.name=mergepilot-copaw 检索。
- 历史证据(R1→SK5 九轮 + WH 前两版)全部保留;本包为收官版。
