# AgentTeams 复赛材料 — 双案例重放包（PR #1 普通协同 + PR #2 高危安全门）

- 阶段: Phase 14.2H-WD-REPLAY-MATERIALS-UPDATE
- 生成: 2026-08-29（UTC 时间戳为准）
- 运行时: Windows Docker Desktop，AgentTeams 官方组件栈（controller/Tuwunel/MinIO/Higress/Element Web），
  源码与镜像锚定 commit `223ddc2`；copaw worker 运行时 `223ddc2-build2`（含存储隔离与同步语义修复）
- 裁决: **REPLAY_MATERIALS_READY_FOR_DEMO_PLATFORM**（见 VERDICT.txt）

## 案例总览

| | PR #1（wd1-pr1-bootstrap） | PR #2（demo/high-risk-human-gate） |
|---|---|---|
| 定位 | **普通多智能体协同案例**：自主 DAG 闭环 | **高危安全门案例**：人工安全门 + 修复验证闭环 |
| 仓库 | nghqqa/fastapi-boilerplate-demo（PR #1） | nghqqa/fastapi-boilerplate-demo（**PR #2，保持 OPEN**） |
| 流程 | review-1 → fix-1 → verify-1（全自主） | review-1 → ⛔人工安全门 → fix-1 → verify-1 |
| 结论 | 全链 SUCCESS（三棒均 TASK_COMPLETED） | Reviewer 确认 CWE-22 高危 → 人工批准 → 修复经独立验证（残余 SEVERITY: NONE） |
| merge | 演示边界外 | **未 merge/push/close/reopen** |

## 文件索引

| 文件 | 内容 |
|---|---|
| 01-case-pr1-normal.md | PR #1 普通协同案例（自主 DAG 全记录与证据指针） |
| 02-case-pr2-high-risk.md | PR #2 高危安全门案例（完整闭环 + 事件 ID） |
| 03-agent-roles.md | Agent 角色矩阵（含人工角色、权限边界、通信与存储协议） |
| 04-dag.md | 双案例 DAG（Mermaid）与状态机 |
| 05-timeline.md | 合并时间线（UTC，含事件 ID 与证据指针） |
| 06-human-gate.md | 人工安全门设计与本次真实门记录 |
| 07-disclosures.md | 工程风险与事件披露（MinIO 清空/tool_guard/状态枚举等） |
| 08-scope-boundary.md | 明确未实现项（PolarDB RAG、Agentic Database Branch、完整 AgentLoop/OTel） |
| 09-demo-scripts/ | 评委可读演示脚本：3 分钟 / 8 分钟 / 15 分钟 |
| 10-demo-platform.md | 演示平台页面设计与状态字段（JSON Schema + 状态机） |
| artifacts/ | 关键工件快照（补丁、结果协议、探针原始输出、人工门记录） |
| SECRETS-SCAN.txt | 最终秘密扫描结果 |
| SHA256SUMS | 本目录全部文件校验和 |
| VERDICT.txt | 裁决 |

## 诚 实 声 明（摘要，详见 07/08）

本材料如实披露三项工程事件（MinIO shared 树异常清空并已恢复、tool_guard 审批超时清空会话、
状态枚举兼容问题），并明确声明：**PolarDB RAG、Agentic Database Branch、完整 AgentLoop/OTel
可观测链路本次未实现**，不得在评审中声称已完成。
