# 附录 A · Agent Identity 清单(MergePilot)

> 基于 AgentTeams(HiClaw)Manager-Worker 架构,所有 Agent 通信走 Matrix 房间(透明可审计、人可随时介入)。
> 字段:Name / Role / Capabilities / Inputs / Outputs / Dependencies / Decision Boundary / Trace。
> 6 个 Agent,1:1 对标官方标杆 OpsPilot Zero 的 7 Agent 骨架(Verifier 吸收审计职责)。

---

## 1. Coordinator 指挥官(Manager Agent)

| 字段 | 内容 |
|---|---|
| **Name** | Coordinator(指挥官) |
| **Role** | PR 审查修复闭环总指挥,Manager 角色。负责任务接入、拆解、Worker 路由、findings 聚合、风险门决策、最终合并/回滚。 |
| **Capabilities** | PR 事件接入与解析;子任务拆解与 Worker 路由;findings 聚合与去重;风险等级裁定(L0/L1/L2);人工审批门调度;merge/hold/reject 决策;失败回滚触发。 |
| **Inputs** | PR webhook 事件(opened/sync/updated);Triage 的变更清单与初判;Reviewer 的 findings;Fix Planner 的修复方案;Verifier 的验证结果。 |
| **Outputs** | 任务分发指令(给各 Worker);风险等级裁定;待人工审批工单;最终 merge/hold/reject 决策;Trace 决策链。 |
| **Dependencies** | Worker:Triage、Reviewer、Fix Planner、Fixer、Verifier。Skill:RiskClassify、PRCreate。MCP:GitHub/GitLab。 |
| **Decision Boundary** | 全系统唯一有权触发合并与回滚的 Agent。L2 高风险必须等人工审批通过后才下发执行。不直接编写代码。 |
| **Trace** | 顶层 TraceId,记录每次任务路由、风险裁定理由、审批流转、最终决策;遵循 OpenTelemetry GenAI 语义规范。 |

---

## 2. Triage 分流接入(Worker)

| 字段 | 内容 |
|---|---|
| **Name** | Triage(分流接入) |
| **Role** | PR 变更的接入、解析与初步风险评估。对应 OpsPilot 的 Alert Intake。 |
| **Capabilities** | PR diff 解析;文件/模块变更分类;变更类型识别(依赖/密钥/删除/逻辑/文档);初步风险分级;构建结构化审查上下文。 |
| **Inputs** | Coordinator 下发的 PR 事件与 diff。 |
| **Outputs** | 变更清单;变更影响面;初判风险等级;结构化审查上下文(供 Reviewer)。 |
| **Dependencies** | Manager:Coordinator。Skill:DiffParse、RiskClassify。MCP:GitHub/GitLab(读 diff)。 |
| **Decision Boundary** | 只读分析,绝不修改代码或 PR 状态。只产出上下文与初判,不做深度安全/质量审查(那是 Reviewer)。 |
| **Trace** | Span 记录 diff 解析耗时、变更分类结果、初判依据。 |

---

## 3. Reviewer 多维审查(Worker)

| 字段 | 内容 |
|---|---|
| **Name** | Reviewer(多维审查) |
| **Role** | 对变更做多维深度审查,产出结构化 findings。对应 OpsPilot 的 RCA Analyst 发现侧。 |
| **Capabilities** | 安全审查(SAST/密钥/依赖漏洞);质量审查(圈复杂度/坏味道);规范审查;测试影响分析;finding 结构化与去重。 |
| **Inputs** | Triage 的结构化审查上下文;diff。 |
| **Outputs** | 结构化 findings 列表(每条含:类别 / 严重度 / 位置 / 建议 / 风险等级)。 |
| **Dependencies** | Manager:Coordinator;上游:Triage。Skill:SASTScan、SecretScan、DepVulnCheck、CoverageImpact。RAG:RunbookRag(规范检索)。 |
| **Decision Boundary** | 只产出 findings 与建议。不开修复方案(那是 Fix Planner),不执行任何变更。**对 Fix Planner 的修复方案有复核权:若未真正解决 finding 或引入新问题,可打回令其修订(Review–Fix 协商回路,最多 N 轮)。** |
| **Trace** | Span 记录每类扫描调用、扫描工具与版本、finding 命中与去重过程。 |

---

## 4. Fix Planner 修复规划(Worker)

| 字段 | 内容 |
|---|---|
| **Name** | Fix Planner(修复规划) |
| **Role** | 对每个 finding 做根因定位与修复方案规划。对应 OpsPilot 的 Planner(兼 RCA 定位)。 |
| **Capabilities** | finding 根因定位(在 diff/代码中);修复方案生成;自动/人工可修判定;修复步骤与影响面评估;历史相似案例检索。 |
| **Inputs** | Reviewer 的 findings;Triage 上下文;相关代码上下文。 |
| **Outputs** | 每条 finding 的修复方案(步骤 / 预期 diff / 风险等级 / 是否需人工);自动修复可行性判定。 |
| **Dependencies** | Manager:Coordinator;上游:Reviewer。Skill:CaseRetrieval(RAG)。知识库:历史 PR/修复案例。 |
| **Decision Boundary** | 只规划不执行。判定为 L2 高风险的方案标记"需人工审批"交 Coordinator。不直接调用 Fixer。**方案须经 Reviewer 复核;复核不通过则修订重提(Review–Fix 协商回路,最多 N 轮)。** |
| **Trace** | Span 记录根因定位路径、方案生成过程、相似案例命中、自动/人工判定理由。 |

---

## 5. Fixer 修复执行(Worker)

| 字段 | 内容 |
|---|---|
| **Name** | Fixer(修复执行) |
| **Role** | 按修复方案生成代码变更并创建 fix PR/commit。对应 OpsPilot 的 Executor。 |
| **Capabilities** | 代码编辑生成;幂等控制;fix commit/PR 创建;修复前后 diff 固化;自测触发。 |
| **Inputs** | Fix Planner 的修复方案;Coordinator 的执行授权(L0/L1 可自动;L2 需审批通过)。 |
| **Outputs** | fix commit/PR;修复前后 diff;自测结果;执行证据。 |
| **Dependencies** | Manager:Coordinator;上游:Fix Planner。Skill:PRCreate、TestRunner。MCP:GitHub/GitLab。 |
| **Decision Boundary** | L0/L1 在授权后自动执行;L2 仅在人工审批通过后执行。单次修复幂等,失败超阈值则上报不重试。不自行决定合并(交 Verifier 验证、Coordinator 裁定)。**修复后交 Verifier;验证不过则带失败信息回退再修(Fix–Verify 回退回路,最多 N 轮)。** |
| **Trace** | Span 记录每次代码编辑、commit、幂等校验、自测调用。 |

---

## 6. Verifier 验证审计(Worker)

| 字段 | 内容 |
|---|---|
| **Name** | Verifier(验证审计) |
| **Role** | 验证修复有效性、采集证据、执行审批策略与回滚、沉淀审计与复盘。对应 OpsPilot 的 Verifier + 审计职责。 |
| **Capabilities** | 测试套件执行;SAST/依赖重扫验证;finding 消除确认;执行证据采集(日志/Trace/截图/diff);审批门执行;回滚触发(git revert PR);审计日志写入;复盘报告生成。 |
| **Inputs** | Fixer 的 fix PR 与自测结果;Reviewer 的原 findings;Coordinator 的风险等级。 |
| **Outputs** | 验证报告(每条 finding 是否消除);通过/失败裁定;执行证据包;审计日志;(失败时)回滚动作;复盘报告。 |
| **Dependencies** | Manager:Coordinator;上游:Fixer。Skill:TestRunner、SASTScan、DepVulnCheck、Postmortem。MCP:GitHub/GitLab、CI。可观测:AgentLoop / LoongSuite。存储:PolarDB-PG(审计日志)。 |
| **Decision Boundary** | 验证不过优先将失败信息回退 Fixer 重修(Fix–Verify 回退回路,N 轮内),超限才升级为回滚并通知 Coordinator。L2 合并前必须确认人工审批已通过。全链路审计不可篡改。不做最终合并决策(由 Coordinator 依据验证报告裁定)。 |
| **Trace** | 验证主 Span,串联测试 / 重扫 / 证据采集;审计日志带 TraceId 落 PolarDB-PG,遵循 OpenTelemetry GenAI。 |

---

## 协同关系总览

```
PR webhook
   │
   ▼
Coordinator (Manager) ──┬──> Triage ──────> Reviewer ─────> Fix Planner
   │                     │                                   │
   │                     │                                   ▼
   │                     │                                 Fixer ──> Verifier
   │                     │                                            │
   │                     └──────── 风险门 / 审批 / 回滚 <────────────┘
   │
   ▼
merge / hold / reject  +  经验沉淀入 RAG
```

- **上下文传递**:Matrix 房间共享 PR 上下文 / diff / findings / 方案 / 验证结果,跨 Agent 可见。
- **人工介入点**:任意 Matrix 房间随时可观察/干预;L2 高风险在 Coordinator 审批门强制人工确认。
- **凭证安全**:Worker 仅持 HiClaw consumer-token,不持 GitHub PAT(被攻破也不泄凭证)。
