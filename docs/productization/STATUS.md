# MergePilot 产品化推进状态（STATUS）

**更新**：2026-09-22（第五轮：M3.5 v3 本地接线与只读可观测闭环）｜ **分支**：`chore/backfill-r3-ops` ｜ 授权范围：M1→M4 至 V0 技术就绪

## 本轮新增：M3.5（验收见 ACCEPTANCE-M35）

| 工作项 | 状态 | 说明 |
|---|---|---|
| v3 adapter 三态接线 | ✅ 实现+测试 | off（默认零开销）/ shadow（真实 PR 只读→分级+计划+状态持久化，零 Agent 零外部写）/ on（授权前按 shadow 并显式记录） |
| 桥派发边界 hook | ✅ 实现+测试 | v3_shadow_hook fail-soft：shadow 炸掉旧链路照常 PROCESSED；桥内不重写任何 v3 逻辑 |
| RunStore（v3_runs 表） | ✅ 实现+测试 | 幂等 UPSERT/重启恢复/取代标记；与票据库分库，状态机唯一（stages.RunStages） |
| 纵向链路（fixture） | ✅ 13 测试 | risk→plan→并行 reviewers→聚合去重保源→finding validation→outcome→store→read model |
| shadow 诚实性 | ✅ 5 测试 | 审查器 SKIPPED 不伪装；diff 不可得档位 FAILED；关键跳过⇒MANUAL_ATTENTION |
| 只读控制台 | ✅ 实现+6 测试 | GET-only（写方法 405）；shadow/fixture 强制标签；部分完成不压缩为成功；交接见 docs/productization/console/STATUS.md |

## 前轮：架构 v3 骨架（设计见 ARCHITECTURE-V3.md，验收见 ACCEPTANCE-ARCHV3）

| 工作项 | 状态 | 说明 |
|---|---|---|
| 架构 v3 文档与状态机 | ✅ 完成 | 11 步流水线；finding validation 与 patch validation 为两个独立阶段；维度状态×run outcome 正交 |
| 风险分级纯逻辑 | ✅ 实现+测试 | Trivial/Lite/Full 可配置规则；敏感路径命中无条件 FULL+人工强制复核；无 LLM 调度 |
| 调度器接口 | ✅ 实现+测试 | DispatchPlanner 唯一调度权；多 PR 上限 1 固化；审查器并发 2（barrier 并行证明）；串行模式 |
| 聚合器+verifier 接口 | ✅ 实现+测试 | 去重保源（精确+Jaccard 近似）；verifier 输入类型构造性隔离推理字段 |
| 降级与预算 | ✅ 实现+测试 | 关键审查器超时→MANUAL 不通过；非关键超时→PARTIAL 可发布；重试预算/全局预算钩子 |
| 控制台字段契约 | ✅ 实现+测试 | 部分完成/降级/超时原因逐字段可见，禁止单一绿色 |
| TicketStore 抽象 | ✅ 实现+测试 | Protocol 固化五操作；SQLite 单实例边界；PG 迁移占位（DECISIONS P-1/#12） |
| feature flag | ✅ 默认关 | MERGEPILOT_REVIEW_V3；v3 未接线 bridge，旧串行链未删 |
| 真实 Agent 接入（v3 第 11 步） | ⬜ 待授权 | R1/R2 批复后最后执行 |

## 历史工作项（第二/三轮）

| 工作项 | 状态 | 说明 |
|---|---|---|
| RAG 全链路审计 + V0 最小闭环 | ✅ 第1/2层 | RAG-AUDIT.md；语料快照绑定（fd34c304…）+ RAG_REQUIRED fail-closed 门；第3/4层待授权 |
| M2 审批：规格+纯逻辑+SQLite 存储+CLI | ✅ 实现层 | 47 测试；真实审批主体/动作启用待 D-1/D-2/D-3 |
| 成本计量脚手架 | ✅ 逻辑层 | 预留/重试累计/结算/并发/崩溃 17 测试；未接真实路径 |
| M1 桥加固（发布语义/恢复/互斥契约） | ✅ 单测级 | 46 测试；场景 3/6/7/9 真实实证待授权 |

## 阻塞重新分类（第三轮定稿）

| 项 | 分类 | 处置 |
|---|---|---|
| P-1 票据存储 | B 已自主决策 | SQLite WAL + TicketStore 接口（PG 迁移路径固化） |
| D-1/D-2/D-3 审批政策 | D 真业务决策 | 机制全实现且拒绝未配置状态；启用待用户 |
| 预算金额 | D 真业务决策 | 接口全实现；未配置不产生消费授权 |
| R1-R4/R7 真实环境 | C/D 外部授权 | 待批；每场景独立判据 |
| R5 镜像层 / R6 usage 源 | C 需授权/选路 | 待批 |
| 密钥轮换 | D 用户挂起中 | M4 出口条件 |

## 里程碑真实状态

- **M1**：单测级收口；真实实证未做——未通过。
- **M2**：规格+存储+CLI 完成；真实审批未启用——未通过。
- **M3**：未开始（待真实 run）。
- **M4**：RAG 第1/2层、成本接口层、v3 本地骨架达成；**V0 技术就绪未达成**，真实内测未开始。

## 下一条可执行动作（按序）

1. **（可选）shadow 证据同步到 MinIO 项目目录**：当前 run 记录在本地 RunStore（~/.mergepilot/v3-runs.db）；如需随证据包走，加导出钩子（本地操作，可自主）；
2. **门 Web 只读页对接 RunStore**：复用 console_v3 读模型 + gate_cli 票据数据（UI 依赖 D-1/D-2 的部分仍关闭）；
3. **待授权批复后**：R7 语料同步 → R1+R2+R4 真实案例轮（v3 shadow 模式将随第一轮真实案例产出对照证据；on 模式真实 Agent 验证在 R1/R2 之内）→ R3 双 head。

## 已验证结果（证据，2026-09-22 实测 @ 工作树）

- `python -X utf8 -m pytest tests/gh_bridge/ tests/console_v3/ tests/orchestrator/ tests/approval/ tests/rag_live/ tests/costmeter/ -q` → **197 passed**（bridge 49 + console 6 + orchestrator 58 + approval 50 + rag_live 17 + costmeter 17；<5s）
- `python -X utf8 -m pytest tests/gh_app/ -q` → 816 passed, 5 skipped（上轮实测；本轮不触其引用面）
- RAG 双副本快照一致（fd34c304…）；M1 九场景 1/2/3/4/5/7/8 ✅单测、9 ✅契约、6 🔒结构保证

## 当前阻塞（外部条件）

- R1-R7 真实环境授权与选择；D-1/D-2/D-3 产品决策；预算金额；密钥轮换（用户挂起）。

## 提交记录（第四/五轮新增，均未 push）

- TicketStore 接口抽象（SQLite 边界+PG 占位）
- 架构 v3 骨架（风险/状态/调度/聚合/verifier/控制台契约/flag + 41 测试）
- M3.5：adapter 三态接线 + RunStore + 桥 hook + 只读控制台（65 新测试）

## 环境事实（2026-09-22 实测）

- 本地栈 8 容器运行中；rag-live 未运行；worker rag_mcp hook 最近激活 2026-09-21。
- 桥运行副本 r3work **未同步**（repo 领先，待 R4/R7 授权后按备份/回退规程同步）。

