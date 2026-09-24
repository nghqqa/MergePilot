# 架构 v3：分级并行审查流水线（ARCHITECTURE-V3）

**日期**：2026-09-22 ｜ **状态**：设计定稿 + 骨架实现（feature flag 默认关闭，旧串行链未动）
**性质**：审查执行模型从固定串行 `reviewer → fixer → verifier` 升级为分级、并行、两段独立验证的流水线。
**不变量**：不自动合并；补丁下载/建议交付；不推送被审查仓库；人工批准后才进入修复；并发默认上限 1；调度权唯一归属。

## 1. 流水线（11 步）

```
1  PR 事件 → 固定 run/仓库/head SHA/RAG snapshot（沿用 run-manifest 既有机制）
2  风险分级（纯规则，可配置，记入 manifest）
3  按风险选审查器集合
4  独立审查器并行提出 findings（单 PR 内并发 ≤2）
5  聚合器去重、排序、保留来源
6  finding validation：独立 verifier 只读 原始 diff + 申报上下文 + findings，
   不读任何 agent 的内部推理
7  发布审查结论 + 覆盖说明（部分完成必须可见）
8  人工门（仅当需要修复）：TicketStore 票据（M2 语义，未拍板不启用）
9  fixer 生成补丁
10 patch validation：独立验证补丁正确性/测试/回归风险（与 6 是两件事）
11 产出补丁下载 + 最终证据（run-manifest / 阶段状态 / 检索证据）
```

**两个验证阶段的区分（不可合并）**：
- **finding validation**（步骤 6）：问题是否真实、可达、可复现——防误报；
- **patch validation**（步骤 10）：补丁是否正确、测试是否通过、无新增回归——防修复引入新问题。
一个 finding 被确认不代表其补丁正确；补丁验证不得引用 finding 验证结论替代自身证据。

## 2. 风险分级（tools/orchestrator/risk.py，纯逻辑）

| 档 | 条件（可配置 RiskRules） | 审查器集合 | 附加 |
|---|---|---|---|
| TRIVIAL | <50 行 且 <5 文件 且 无敏感路径 | 通用 1 个 | — |
| LITE | 50–500 行 或 5–20 文件，无敏感路径 | 通用 + 1 专业 | — |
| FULL | >500 行 或 >20 文件 或 **命中任一敏感路径** | 全部已启用专业审查器 | **human_review_required=true** |

- 敏感路径默认集：`auth / crypto / permission / credential / migration`（子串匹配，可配置扩充）；
- **敏感路径命中 → 无条件 FULL**（小 diff 不豁免）；
- 行数 = additions+deletions 合计；理由（reasons）逐条可解释，随 manifest 落盘。

## 3. 阶段状态与 run 级 outcome（tools/orchestrator/stages.py）

**维度状态（每个 run × 每个审查/验证维度独立记录）**：
`PENDING / RUNNING / SUCCEEDED / FAILED / TIMEOUT / SKIPPED / NOT_APPLICABLE / CANCELLED`
（SUCCEEDED/SKIPPED/NOT_APPLICABLE/CANCELLED 不可逆；FAILED/TIMEOUT 仅可在重试预算内回 RUNNING，预算耗尽后停留态即报告态；attempts 记录重试。）

**run 级 outcome（独立枚举，由 `derive_outcome()` 显式派生，绝不覆盖维度状态）**：
`REVIEW_COMPLETED / REVIEW_PARTIAL / CONCLUSION_PUBLISHED / AWAITING_APPROVAL / FIXING / PATCH_VALIDATING / WRITEBACK_OK / WRITEBACK_FAILED / MANUAL_ATTENTION`

派生规则（摘要）：
- 任一非 CANCELLED/NOT_APPLICABLE 维度处于 TIMEOUT/FAILED/SKIPPED → 不得 REVIEW_COMPLETED，只能是 REVIEW_PARTIAL（附 coverage.missing 明细）；
- **关键安全审查器**（config.critical_reviewers）TIMEOUT/FAILED → 不得发布为通过；只能 REVIEW_PARTIAL 或 MANUAL_ATTENTION；
- 审查完成 ≠ 结论已发布 ≠ 回写成功——三者独立判定。

**降级策略（显式配置，不自动换模型）**：单 agent 超时/模型不可用 → 按 `degradation_policy ∈ {delay, degrade, manual}` 处理；degrade=标 TIMEOUT/SKIPPED 并发布覆盖不足；run 硬上限（hard_deadline）与每阶段重试预算（max_attempts）由配置给出，耗尽 → 对应阶段 FAILED + outcome 转人工。

## 4. 调度（tools/orchestrator/scheduler.py）

- `DispatchPlanner.build_plan(run_ctx, risk, config)` → 有序 PlanStep 列表（审查器并行组→聚合→finding 验证→发布→门→fixer→补丁验证→交付）；
- **并发上限**：多 PR 全局 = 1（`MAX_PR_CONCURRENCY`，完成两路并发测试并获授权前不调高）；单 PR 内审查器并发 = 2（可配 1=全串行）；一个 worker 同一时间只执行一个任务；全部并发任务共享全局模型/资源预算（对接 costmeter 预算守卫）；
- **调度权唯一归属**：run 的派发决策只能出自 DispatchPlanner；bridge 是现网执行器，v3 在 flag 关闭时不产生任何派发行为——**不形成第二套调度事实源**；隔离靠 run 的独立工作目录/会话/证据路径/消息空间（M3 验证对象），不靠改任务名。

## 5. 聚合与 finding verifier（aggregate.py / verify_finding.py）

- 聚合：确定性去重（同路径同类别 + 标题词集 Jaccard ≥ 0.5 合并），severity 取最大，**sources 保留全部来源 reviewer**；输出含 dropped_duplicates 便于审计；
- finding verifier 接口：输入只有 `VeriferInput(finding, diff, allowed_context_paths)`——**类型上不存在其他 agent 的推理字段**（结构性隔离，测试固化）；判定 CONFIRMED/REFUTED/INCONCLUSIVE + 证据。

## 6. 存储抽象（tools/approval/store.py）

```python
class TicketStore(Protocol):        # 接口：create/get/active_for/transition/close
class SQLiteTicketStore(TicketStore) # V0 单实例实现（原 SqliteTicketStore，别名保留）
class PostgreSQLTicketStore(TicketStore)  # 迁移占位（NotImplementedError + DSN 形状）
```

**SQLite 边界（明确且不承诺）**：单 Controller、单部署实例、无多实例高可用、**不承诺多用户 SaaS**。未来多 Controller/多用户/共享部署 → 必须迁 PostgreSQLTicketStore；接口已固定，迁移只换适配器，M2 语义单测全部复用。

## 7. 控制台字段契约（console_contract.py）

`build_console_payload()` 固定输出：risk_level、reviewers[]{name,status,detail}、findings{count,by_source}、finding_validation{status,confirmed,refuted,inconclusive}、coverage{complete,missing[]}、degradations[]{stage,reason}、fixer.status、patch_validation.status、github_writeback.status、rag{snapshot_id,evidence_refs}、manifest{run_id,...}。
**禁止把部分完成/降级压缩成单一绿色"成功"**：coverage.complete=false 时 payload 顶层必须可见（测试固化）。

## 8. 实施顺序与现状

| # | 项 | 状态 |
|---|---|---|
| 1 | 本文档 | ✅ |
| 2 | 状态机与数据结构 | ✅ stages.py |
| 3 | 风险分级纯逻辑 | ✅ risk.py |
| 4 | 调度器接口 | ✅ scheduler.py（串行默认 + 并发2 执行器） |
| 5 | 聚合器纯逻辑 | ✅ aggregate.py |
| 6 | finding verifier 接口 | ✅ verify_finding.py |
| 7 | feature flag 三态接线 | ✅ off(默认)/shadow/on；**桥派发边界已接 shadow**（v3_shadow_hook，fail-soft），on 仍不接真实 Agent |
| 8 | 两审查器并行本地测试 | ✅ tests/orchestrator/ |
| 9 | 超时/部分完成/降级测试 | ✅ tests/orchestrator/ |
| 10 | 控制台字段契约 | ✅ console_contract.py |
| 11 | 真实 Agent 接入 | ⬜ 最后（R1/R2 授权后）；本地纵向链路已通（adapter fixture/shadow，M3.5） |

旧串行链（bridge process/conclude）**在新流程通过回归并获接线授权前不得删除**。

## 9. 验收矩阵（详见 ACCEPTANCE-ARCHV3）

路由三档/敏感升级/并行/去重保源/verifier 隔离/单超时可发布/关键超时不可通过/run 硬上限/重试预算耗尽/PR 更新失效/门后 fixer/两验证独立/RAG 快照一致/SQLite 边界/控制台部分状态——全部有对应测试（本地面）。真实 Agent、真实 RAG、真实 GitHub 集成为独立未验证项，**测试通过不冒充生产验证**。
