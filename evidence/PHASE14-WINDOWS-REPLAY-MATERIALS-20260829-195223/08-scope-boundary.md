# 范围边界声明（未实现项 — 防止过度声称）

> 评审口径：以下能力在本次复赛演示中**未实现/未接入**。任何材料与答辩不得声称
> 已完成。它们属于整体方案规划，待后续阶段落地。

## 明确未实现

| 能力 | 状态 | 说明 |
|---|---|---|
| **PolarDB RAG 接入** | ❌ 未实现 | 未接通 PolarDB 作为向量/知识检索后端；当前 Agent 的知识来源仅为任务 spec、GitHub 公开内容与仓库内文档。无任何检索增强链路在运行。 |
| **Agentic Database Branch（数据库分支）** | ❌ 未实现 | 未实现基于数据库写时分支（COW/snapshot）的 agent 隔离数据环境；演示中的"隔离"仅为**文件与容器层**（task 目录、独立 venv/clone），不含数据库分支。 |
| **完整 AgentLoop / OTel 可观测** | ⚠️ 部分实现（2026-08-29 更新）| 已实现：**live-cloud 双轨**——(a) evidence-replay Trace（历史证据回放，02 案例与 AGENTLOOP-OTEL 目录）；(b) **阿里云 AgentLoop live Trace 已验证**（`AGENTLOOP_ALIYUN_LIVE_TRACE_VERIFIED`：四角色真实运行经 OTLP 手动埋点 + 本地转发器上报，6 批次全 200）。未实现：ARMS 探针自动埋点（需 VPC）、OTel metrics/仪表化、trace 与日志的自动关联 UI。 |

## 已实现（可声称）的能力边界

- AgentTeams 官方组件栈上的多智能体协同：Matrix 通信、共享任务存储、
  Leader 编排、四角色协作、消息幂等（mentions/txn/event ledger）
- 高危安全门拓扑：协议化审查结论触发受控停等 + 人工批准落盘 + 批准后自主派发
- 修复-验证分离：Fixer 最小修复+本地测试；Verifier 独立重 clone 复现与回归
- 工程自证：同步语义缺陷的根因审计、修复与 12 项单测（零回归）、事件级时间线

## 常见追问的边界答复

- "RAG/向量检索在哪一步用到了？" —— 没有使用；审查与修复依据是 PR diff、仓库测试与探针。
- "数据库分支隔离在哪体现？" —— 未体现；本演示的隔离是文件系统/容器级。
- "有 OTel trace 吗？" —— 没有；日志级可观测，OTel 属后续工作。
