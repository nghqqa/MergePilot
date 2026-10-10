# QwenPaw 迁移评估（独立事项——不预设结论，本轮不替换运行时）

- 日期：2026-10-11（rc.24 轮建立）
- 性质：迁移可行性评估。依据官方仓库证据 + 本栈 itest 实证；**不预设 QwenPaw 已解决任何问题**。
- 背景：copaw 栈 itest 四场景定案（MANIFEST §6）——无 durable 模型配置路径（模板硬编码 broken gateway+周期重推+无 spec 覆盖位）+ get_soul 上游错配。

## 1. 官方证据（README.zh-CN + v1.2.1/v1.2.4 Release Notes，2026-10-10 查证）

| 项 | 官方陈述 | 对本栈三个已知缺陷的**假设性**对应（须隔离验证，不预设已解决） |
|---|---|---|
| 运行时统一 | v1.2.1："Manager 与 Worker 运行时栈统一到 QwenPaw 2.0.1；用 QwenPaw 插件系统替代物理 Matrix channel overlay（不再 monkey-patch CoPawAgent）" | get_soul 缺陷在 copaw_worker 桥接层——插件化重写**可能**消除，须验证 |
| Matrix 凭据 | v1.2.1："直接从 agent.json 读 Matrix 凭据，不依赖运行时 import copaw" | 配置加载路径**可能**改变，是否仍存在"模板覆盖"须验证 |
| 配置管理 | v1.2.4："Manager/Worker 升级至 QwenPaw 2.2.1；向 Worker 传递模型能力配置；运行时热更新" | **未见面世证据**表明 LLM provider 模板生成机制改变——broken gateway 问题上不预设 |
| 状态迁移 | v1.2.1："增强 CoPaw 到 QwenPaw 的状态迁移" | 迁移工具存在性=线索，能力边界须验证 |

## 2. 本栈迁移影响面（初判，须逐项核实）

| 层 | 影响 |
|---|---|
| worker 镜像 | copaw-worker 223ddc2 → qwenpaw-worker（官方多架构镜像，版本随 AgentTeams 版本） |
| ctrl | embedded 223ddc2 是否兼容 QwenPaw worker——**或需整体升级 AgentTeams**（涉及 Matrix/MinIO/路由全栈） |
| provision 脚本 | recover-workers.sh 的 takeover/reconcile 流程假设 copaw runtime——需 QwenPaw 等价命令 |
| MergePilot 侧 | agentteams-executor 的 REST API 契约（workers/projects/tasks/summary）——版本升级后须回归 |
| Matrix 协议 | sendTaskDelegation/collectReply 的消息格式——QwenPaw 插件是否兼容现有 envelope/marker 语义 |

## 3. 隔离验证设计（迁移决策前置门）

1. itest 栈模板升级到目标 AgentTeams 版本（QwenPaw 线）；
2. 四 worker 以 QwenPaw 运行时 provision；
3. **逐项重跑本栈已知缺陷**：模型配置持久性（S1/S2 对照——收敛后是否被周期覆盖）、重启/重建后配置存活、get_soul 等价缺陷不存在；
4. recovery-drill.mjs 的 Matrix 往返（marker 关联+真实 LLM）全绿；
5. MergePilot 侧：mu-job-consumer 集成 + 一次完整修复 dry-run（真实 Gitee PR）；
6. 全部通过才形成迁移建议；任一失败如实登记并评估 workaround。

## 4. 决策框架

- 若隔离验证全过：迁移收益=durable 配置+上游维护线（copaw 已停更）；成本=一次全栈升级窗口。
- 若部分失败：评估 workaround 边界（如 QwenPaw 仍需 gateway 修复）后再决策。
- 本评估不阻塞任何当前生产事项；copaw 栈过渡期靠 recover-workers.sh（临时收敛，PR #403）。
