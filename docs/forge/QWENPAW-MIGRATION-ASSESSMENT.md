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

---

## 5. 隔离验证结果（2026-10-11 收尾波回填——§3 设计逐项）

> 完整证据：r3work/qwenpaw-eval-20261010/（REPORT.md + b3/b5 探针 + 迁移/回滚记录）。
> 隔离栈：qwp 前缀（独立网络/卷/端口/Matrix homeserver/凭据；LLM 真源=生产 worker env 的现役 deepseek key，经隔离 Higress 网关路由）。

| §3 设计项 | 结果 | 关键证据 |
|---|---|---|
| itest 栈升级到 v1.2.4（QwenPaw 线） | ✅ | 5 镜像 digest 锁定（v1.2.4/QP 2.2.1 官方配对）；apply/verify 契约脚本入库（deploy/agentteams/qwenpaw-v124/） |
| 四 worker provision | ✅（MergePilot 形态） | MergePilot ensureFourAgents（v1.2.4 双代契约适配后）对现役隔离 ctrl 实测 ok+读回一致 |
| **模型配置持久性（S1/S2 对照）** | ✅ **根因消除** | v1.2.4 worker 配置=controller runtime.yaml desired state 唯一真源；重建/重启/重推周期后实测配置仍为期望态（B5 S4：mtime 重推 02:46→02:52，重推后往返 PASS）——旧栈"周期重推覆盖 patch"问题在体系上不存在（用户 patch 正位=Higress provider，持久于 ctrl 数据卷） |
| 重启/重建后配置存活 | ✅ | worker 重启/仅容器重建/ctrl 重启三场景 mp_task 真实往返全 PASS（B5 5/5） |
| get_soul 等价缺陷不存在 | ✅（等价能力实证） | SOUL/AGENTS/IDENTITY 双投影（根存储合同+原生 workspace）在位；生产状态副本迁移后 worker 以身份内容运行（迁移期 verifier 角色一致语义回复）。登记：qwenpaw **独立** worker 无 Matrix 通道（channel_names=-），mp_task 消费需 team 成员身份——生产 worker 均为 team 成员，非阻塞 |
| recovery-drill 等价（marker 关联+真实 LLM） | ✅ | mp_task 信封在 copaw/qwenpaw 双 runtime 下真实回复 marker+契约 JSON；迟到回复隔离/短窗收尾/逐轮 marker 独立（B3） |
| MergePilot 集成回归 | ✅（适配后） | v1.2.4 worker create/PUT 扁平契约适配（union body+AT_WORKER_DRIFT 读回防线）+ cancel 404 幂等语义——QC 8/8，后端 275/0 |
| 完整修复 dry-run（真实 Gitee PR） | ⏸ 未完成 | 需隔离 Console 全链（见 §5.1 未过门） |

### 5.1 结论与剩余上线门（GO with gates）

**GO 前提已成立**：迁移可行性、官方热切换路径（PUT runtime）、迁移/幂等/回滚、生产状态副本迁移、双 runtime 真实往返、MergePilot 兼容适配均已实证。**剩余门（缺一不上）**：
1. MergePilot 执行器适配 PR 合入（本轮交付：feat/agentteams-v124-compat 分支）；
2. ≥24h 隔离 soak（多轮 reconcile 配置稳定性）——**未完成，保持未过门**；
3. 真实委派任务（leader/TeamHarness 链）上的 cancel 行为实测；
4. Manager（openclaw→qwenpaw）存量状态迁移演练（新初始化已验证；生产 manager 已停用、风险有限）；
5. 生产切换窗口按 MANIFEST-qwenpaw-v124.md §6 顺序执行（controller 先行+原数据卷；worker 逐个热切换+切换前 MinIO 前缀快照；禁止删除重建式迁移）。

组件版本分记：Console（现役 rc.24，与执行器栈独立回滚）／AgentTeams（候选 v1.2.4=8765d697）／worker runtime（候选 qwenpaw 2.2.1@cae57737；现役 copaw 223ddc2-v5fix-shim）。
