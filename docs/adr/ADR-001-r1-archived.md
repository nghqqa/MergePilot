# ADR-001：单一 AgentTeams Review 架构审计与恢复方案

- 日期：2026-10-02
- 基线：main=`b01a91de84d978dcb4594fa6c74771312f10af65`；beta.5 RC digest=`sha256:fd153d9e…ac9007`（审计期间未变化）
- 约束遵守：全程只读（零代码/DB/迁移/付费调用/实例改动/Release/邀请/保护规则）
- **最终裁决：ARCHITECTURE_DRIFT_CONFIRMED**

---

## 1. 当前真实架构（代码实证 @b01a91de）

```
GitHub webhook → mu.job(event_sync)
  └→ executeEventSync (api.mjs:107)
      ├─ PR 快照 upsert（protection 恒写 'unknown'）
      └─ handlePullRequestEvent (review-service.mjs:52)
          ├─ 幂等建 run（RECEIVED→REVIEW_QUEUED→REVIEWING）
          ├─ 【第一层 reviewer】claim agent_role='reviewer' provider='deterministic'   ← 漂移点①
          │    输入：fetchPrContext 原始 diff（GitHub API）
          │    输出：reviewDiff 正则 findings → mu.agent_finding
          ├─ 【第一层可选 LLM】claim agent_role='reviewer' provider='openai_compatible' ← 漂移点②
          │    输入：sanitizeContextForLlm(pr+findings+diff 白名单裁剪)
          │    输出：LLM-* findings → mu.agent_finding（policy 开关控制）
          └─ 【本地 Leader 门禁】advanceAfterReview (leader.mjs:38)
               输入：findings + protection(fetchPrContext 探测，不落库)
               decideAfterReview：protection≠configured→BLOCKED / P0P1→fix_required / clean→COMPLETED
               （纯策略函数，无 LLM，无 attempt 行）
  └─ 若 fix_required → fixVerifyRound (fix-orchestrator.mjs:29)
      ├─ 预 claim fixer/reviewer/leader 三 attempt（provider='agentteams'）
      ├─ 【第二层 Matrix 串行】输入=sanitizeBrief(findings)（rule_id/severity/path/masked——零代码）
      │    reviewer：schema {findings:[{severity,path,summary}]} → 结果仅存 output_digest，不入 finding 表 ← 漂移点③
      │    leader  ：advisory 建议 → 结果仅存 digest（本地 Leader 才是权威）
      │    fixer   ：{suggestion,patch_hint} → mu.fix_attempt(DRY_RUN)
      │    verifier：{verdict: PASS|FAIL|BLOCKED} → mu.verification_attempt
      └─ 【本地 Leader 终裁】advanceAfterVerify(verdict) → VERIFIED→COMPLETED/BLOCKED/REWORK
```

### 六入口清单

| # | 入口 | API/job | run 创建 | 调用角色 | 读原始 diff | GitHub 写 |
|---|---|---|---|---|---|---|
| 1 | webhook 自动 | POST /api/mu/github/webhook→event_sync | handlePullRequestEvent | det reviewer(+LLM)→本地 Leader→(fix_required 时)四 Agent | 是（第一层） | 否 |
| 2 | 前端"触发只读审查" | POST /api/mu/prs/:id/review→job(review_run) | enqueueJob→tick 认领 | **fixtureReviewRun（合成）** | 否 | 否 |
| 3 | fixVerifyRound 直调 | （内部，被 1/6 复用） | 不建 run | 四 Agent Matrix | 否（脱敏 brief） | 否 |
| 4 | job tick | POST /api/mu/jobs/tick | —（消费 event_sync/review_run） | 同 1/2 | — | 否 |
| 5 | 测试 fixture | MU_FIXTURES=1 | fixtureReviewRun | 零真实执行 | 否 | 否 |
| 6 | 运维补跑 | 容器内直调 fixVerifyRound | 不建 run（复用现有） | 四 Agent | 否 | 否 |

## 2. 既定产品语义逐项核对

| # | 目标语义 | 现状 | 判定 |
|---|---|---|---|
| 1 | deterministic=precheck 非正式 Reviewer | agent_role='reviewer'+provider='deterministic'，UI 显示"Reviewer（审查）· 规则引擎" | **DRIFT**（dd2fc44，Wave 3 PR-B 引入时即此形状） |
| 2 | AgentTeams Reviewer=唯一正式 Reviewer | 它在第二层、只看脱敏 findings、产出不入 finding 表 | **DRIFT**（5bddf53 Wave 3.3 定位为"语义审查建议"） |
| 3 | 四角色同一 review run | 同一 run_id ✓（attempt 表共域） | **MATCH** |
| 4 | Fixer 仅在需要修复时运行 | fix_required 才进 fixVerifyRound ✓ | **MATCH** |
| 5 | Verifier≠代码测试通过 | verdict schema+文案"建议验证" | **MATCH**（runbook/notes 已披露） |
| 6 | protection unknown 禁可合并结论、不禁只读审查 | unknown→BLOCKED（含无 finding 的 clean PR 也 BLOCKED——**过度阻断**：审查链不再执行） | **PARTIAL** |
| 7 | 数据边界明确 | 第一层读原始 diff；第二层仅 sanitizeBrief ✓；protection 探测结果**不落库**（决策 rationale 才有） | **PARTIAL** |
| 8 | 无自动 approve/merge | 结构性禁止 ✓ | **MATCH** |

## 3. PR #8 真实时间线（run b772c2c8，只读取证）

```
14:49:15.232  run 创建（webhook，RECEIVED）
14:49:15.241  ── 预检开始 ── reviewer#1 claim（deterministic）
14:49:19.0~   reviewDiff→2 findings（R-SECRET P0 / R-SQL-CONCAT P1）落库
14:49:19.074  orchestration_decision: review_done=findings_present (v1)
14:49:19.076  ── 门禁结论形成 ── 本地 Leader: fix_required (leader-v1)
              （此刻已决定"需要修复"——后续全部是建议链）
14:49:19.443  ── 建议链开始（预 claim fixer/reviewer#2/leader）
14:49:19.445  reviewer#2 (agentteams)：对 sanitizeBrief 的风险复核——正式审查价值缺失
14:49:19.450  leader (agentteams)：advisory——权威在本地的 advanceAfterVerify
14:49:19+5s   fixer (agentteams)：DRY_RUN 建议→mu.fix_attempt
14:49:39.575  verifier (agentteams)：建议一致性判断（≠代码测试）
14:49:39.590  leader_final=verified_complete（本地 Leader 终裁，权威）
14:49:39.593  run COMPLETED
```

关键读数：protection 探测在 review 阶段完成且结果已知（否则 fix_required 不可能出现——unknown 走 BLOCKED 分支）；`pull_request.branch_protection_status` 恒 'unknown' 是快照字段从不回写（发现：**protection 探测结果未持久化**）。

## 4. 十问速答

1. **deterministic 为何记 reviewer attempt**：Wave 3 PR-B（dd2fc44）初版即无 precheck 概念——规则审查即审查；后续层叠未回头改角色。
2. **AgentTeams reviewer 审原始代码？** 否——仅 `sanitizeBrief`（rule_id/severity/path/masked ≤20 条），代码零出站。
3. **第一层 LLM 接收/输出**：`sanitizeContextForLlm`（pr 元数据+findings+diff 白名单裁剪 ≤24KB）→ `LLM-*` findings 入 agent_finding（与规则 findings 同表混存）。
4. **AgentTeams reviewer 会产生新发现？** schema 允许 findings，但结果只存 attempt.output_digest——**不入 finding 表、不进 Leader 输入**（实际为死端输出）。
5. **Leader 执行两次？** 本地 Leader（策略函数，门禁+终裁，无 LLM/attempt）vs AgentTeams leader（advisory，有 attempt）。**权威=本地**（advanceAfterReview/advanceAfterVerify 都是本地）；AgentTeams leader 建议仅存档。严格说执行了 2 次本地决策+1 次 advisory。
6. **PASS/COMPLETED/BLOCKED 归属**：PASS=verifier 对修复建议的判定（建议链事实）；COMPLETED=本地 Leader 终裁（run 生命周期事实）；BLOCKED=门禁或终裁的阻断事实（两个语义共用一词——见 Q7）。
7. **同 PR/head 冲突结论可能？** 可能且真实存在：门禁 BLOCKED（protection unknown）+ 历史建议链 COMPLETED 可共存于同 PR 的不同 run；同 run 内不会（状态机线性）。前端曾混显（已在 #283 收敛为分层展示）。
8. **哪个状态决定可合并？** 无——**不存在合并路径**（结构性禁止）；最接近的是 run.status=COMPLETED 且 protection=known_clean，但也仅是"审查完成"事实。
9. **protection unknown 为何阻断建议链？** `decideAfterReview` 把 unknown 与"需修复"正交组合成同一 early-return——clean+unknown 也 BLOCKED，建议链不再执行。这是把"禁可合并结论"扩大为"禁一切后续"的过度阻断（代码 leader.mjs:39-41）。
10. **建议通道开关控制哪段代码**：`mu.agent_policy.mode/enabled` → run 创建时冻结快照 → `resolveEffectiveLlmPolicy` → review-service 内 `if (effLlm.kind !== 'disabled')` 的第一层 LLM claim 段。不触碰 AgentTeams。

## 5. 三方案比较与推荐

| 维度 | A：只改命名 | B：det 降 precheck+AT 完整四角色 | **C：det 出 findings+AT 从 Leader 起**（推荐） |
|---|---|---|---|
| 产品语义 | 不解决（两层 reviewer 仍在） | 完全对齐，但 AT reviewer 仍只看 brief | 对齐且诚实：AT reviewer 删除（它本就是死端） |
| 安全边界 | 不变 | 不变 | 不变（甚至收窄——少一次出站） |
| 模型成本 | 4 次/链 | 4 次/链 | **3 次/链（-25%）** |
| 延迟 | ~21s | ~21s | **~16s（-5s）** |
| AT reviewer 独立价值 | — | 唯一正式审查（但它看不到代码，价值虚） | 承认现状：它复核的是第一层 findings 的摘要——该价值并入 Leader 输入 | 
| 状态机 | 不变 | 加 PRECHECK 态 | 加 PRECHECK 态+Matrix 轮次少一 |
| 数据迁移 | 零 | attempt role 重分类 | 同左 |
| 历史兼容 | 零风险 | 前端按 provider 分组显示"历史预检" | 同左 |
| 回滚 | 平凡 | flag | flag |
| 测试 | 文案级 | 中 | 中（删一角色 schema） |

**推荐 C**，理由：Q4 已实证 AgentTeams reviewer 的输出是死端（不入 finding、不进决策）——方案 B 把它升为"唯一正式 Reviewer"要么名不符实（仍看 brief），要么必须把原始 diff 出站给它（**违反安全边界第 7 条**）。C 是唯一"语义对齐+安全不变+成本下降"的选项。若未来希望 AT 真正审查代码，需先设计代码出站的脱敏通道，另立 ADR。

## 6. 方案 C 设计要点

**新状态机**：
```
RECEIVED→PRECHECK_QUEUED→PRECHECKING→[precheck findings]→REVIEW_QUEUED→REVIEWING
  （本地规则+可选 LLM，stage='precheck'，不占 agent_role）
→ AgentTeams Leader 裁定（吸收原 reviewer brief 复核职能）
→ FIX_QUEUED→FIXING（fixer）→VERIFY_QUEUED→VERIFYING（verifier）→VERIFIED→COMPLETED/BLOCKED
protection unknown：门禁结论=不可合并，但 REVIEW/FIX/VERIFY 照常执行（只读）→ run 终态=REVIEWED_NO_MERGE（新增）
```

**关键决策**：
- precheck 存 attempt 但 `stage='precheck'`（新增列或复用 prompt_version 前缀），`agent_role` 废弃该场景——不冒充 reviewer
- 历史数据不重写：前端按 `provider='deterministic'` 渲染"历史预检记录"标签
- LLM policy 开关：**建议删除**（第一层 LLM 与 AT leader 的语义审查重复；如保留则重定义为 precheck 增强）
- protection 探测结果落库至 `pull_request.branch_protection_status`（修 Q7 发现）
- feature flag `MU_REVIEW_ARCH=v2`（default v1）+ 回滚=关 flag
- 无 DB 破坏性迁移：新增 stage 列可空 + 状态域扩一枚举值

## 7. 测试矩阵（15 项）

干净 PR / det P0P1 / 仅 LLM 语义发现 / protection known_clean / **protection unknown（新语义：审查执行+不可合并结论）** / AT 不可达（readiness gate→FIX_QUEUED 挂起恢复）/ LLM 超时（fail-closed 回落）/ fixer 失败（全 roleClaims FAILED+死信）/ verifier FAIL（REWORK）/ webhook 重复（幂等）/ server 重启（FIX_QUEUED 恢复——本轮已实证）/ 跨租户（零串线）/ **历史双 reviewer run 展示**（旧数据+"历史预检"标签）/ 手动重试 / 全程零 GitHub 写

## 8. 实施拆分

- **PR A**（后端）：stage 列+状态域扩展+precheck 化+AT 轮次删 reviewer+protection 落库+flag —— 核心变更，含迁移测试
- **PR B**（前端）：阶段分组（预检/审查/修复/验证）+历史预检标签+policy UI 删除/重定义
- **PR C**（兼容）：历史 run 回放测试+双 reviewer 旧数据展示快照+runbook 更新

依赖：A→B→C；每步独立可合入、可回滚。
