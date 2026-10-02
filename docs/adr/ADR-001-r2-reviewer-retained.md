# ADR-001-r2：单一 AgentTeams Review 架构（修订版——Reviewer 保留）

- 日期：2026-10-02（r2 修订，替代 r1 的方案 C）
- 基线：main=`b01a91de84d978dcb4594fa6c74771312f10af65`（只读，未实施）
- **最终裁决：REVIEWER_RETAINED_AND_DESIGNED**（附输入边界决策项待维护者确认）

---

## 0. r1 撤回声明

撤回 r1 方案 C 的"删除 AgentTeams reviewer 轮次"。r1 的错误：把"当前实现中 Reviewer 输出是死端"这一**实现缺陷**误判为"Reviewer 产品价值不存在"。产品语义裁决：**AgentTeams Reviewer 是正式审查主体**，实现缺陷应被修复，而非角色被删除。

保留的事实基础（r1 审计证据仍然成立，作为缺陷清单）：
- D1：Reviewer 输入仅 sanitizeBrief（rule_id/severity/path/masked）——无代码上下文
- D2：Reviewer findings 只存 output_digest——不入 agent_finding、不进 Leader 输入（死端）
- D3：deterministic 被记为 agent_role='reviewer'（冒充正式角色）
- D4：第一层独立 LLM 通道与 Reviewer 职能重叠
- D5：protection unknown 阻断审查链（过度阻断）
- D6：protection 探测结果不落库

## 1. 目标架构（既定）

```
GitHub PR
→ deterministic precheck（本地、快速、规则、非 reviewer 角色）
→ AgentTeams Reviewer（唯一正式审查主体）
→ Leader（消费 precheck+Reviewer findings；protection unknown 禁可合并结论、不禁审查）
→ 必要时 Fixer DRY_RUN
→ Verifier（建议一致性，≠代码测试通过）
→ 门禁结论与建议链结果分别保存
```

## 2. 安全输入方案比较（Reviewer 代码上下文）

| 维度 | A：仅脱敏 findings | **B：白名单+redaction+限量的 diff 片段（推荐）** | C：本地模型/可信边界读全 diff |
|---|---|---|---|
| 违反代码不出站 | 否 | **不违反**（出站的是 redacted 片段——与既有 sanitizeContextForLlm 同族，边界从"零代码"修订为"零未脱敏代码"） | 否（本地执行） |
| 能发现 precheck 未发现问题 | **否**（无代码即无语义审查） | **能**（有限但真实：逻辑缺陷/不安全模式在片段内可见） | 能（完整） |
| secret redaction 失败模式 | n/a | **残留风险**：正则漏检的非常规 secret 形状——缓解：双重 redaction（发送前+Provider 侧承诺不留存）+ 泄漏哨兵持续扫描 | n/a |
| 模型成本/延迟 | 1 次调用/~5s | 1 次/~8-12s（payload 大） | 高（本地推理或 TEE 部署成本） |
| 审计与保留 | 出站=摘要（低敏） | 出站 payload 存 digest+长度（不存原文）；审计记片段数/字节 | 本地，无出站面 |
| Beta 可实现性 | 即刻 | **即刻**（复用 LLM_EGRESS_LIMITS 架构+新增 redaction 管道） | 不可行（Beta 无本地模型设施） |

**选择 B**，理由：A 使 Reviewer 只能是"风险证据审查"（语义降级——不满足既定"正式审查主体"）；C 超出 Beta 基础设施；B 以可审计的脱敏边界换取真实的有限代码审查能力。**边界修订需维护者确认**（见裁决）。

### B 的具体边界（Reviewer 输入构成）

```
Reviewer 输入 = {
  precheck_evidence: [ {rule_id, severity, path, line, masked_summary} ],   // 既有 sanitizeBrief
  code_context: {                                                          // 新增
    files: [ ≤5 个 finding 命中文件 ],
    per_file: [ 以 finding 行为中心 ±30 行 diff 片段, 每文件 ≤200 行 ],
    transforms: [ secret-redaction（正则族+熵检测双通道）, 行内 token 掩码, 路径规范化 ],
    limits: { total_bytes ≤ 24KB（复用 EGRESS 限制）, binary/lockfile 排除 }
  },
  pr_metadata: { title≤200, changed_files 数, 无 body/author }
}
```

## 3. 数据流图（目标态）

```
webhook → run(RECEIVED)
→ PRECHECK_QUEUED → PRECHECKING（本地规则；stage='precheck'，无 agent_role）
   └→ precheck_findings → mu.precheck_finding（新域，不再写 agent_finding）
→ REVIEW_QUEUED → REVIEWING
   └→ AgentTeams Reviewer（brief+code_context 出站；schema 含 confirm/reject/new_findings）
      └→ 输出三分类：
         confirmed → agent_finding（source='reviewer'）
         rejected  → orchestration_decision(stage='review_disposition', decision='rejected:<rule_id>')——不删 precheck 行
         new       → agent_finding（rule_id='AT-*', source='reviewer'）
→ Leader（本地策略+advisory？——双 Leader 问题一并收口：Matrix leader 轮保留为 advisory，
   本地 advanceAfter* 为权威；Leader 输入=precheck+reviewer findings 合并视图+protection）
   └→ decision 逐条记录采用/拒绝/冲突（decision.payload.finding_dispositions）
→ [fix_required] → FIX_QUEUED→FIXING（Fixer DRY_RUN）
→ VERIFY_QUEUED→VERIFYING（Verifier 建议一致性）
→ 本地 Leader 终裁 → VERIFIED→COMPLETED / BLOCKED / REVIEWED_NO_MERGE(新：protection unknown)
门禁结论与建议链结果分别保存：run.gate_outcome（新列）≠ run.status
```

## 4. Reviewer findings 持久化设计

- 新表 `mu.reviewer_finding` 或复用 `mu.agent_finding` + `source` 列（'precheck'|'reviewer'，推荐后者——迁移最小）
- Reviewer 输出 schema（v2）：
  ```
  { dispositions: [ {rule_id, action: 'confirm'|'reject'|'amend', amended_severity?} ],
    new_findings: [ {severity(P0-P3), path, line_start?, summary≤200, evidence_span?} ] }
  ```
- **强制写入路径**：fixVerifyRound 中 reviewer 轮结果必须经 `persistReviewerFindings()`（落 finding 表+decision）——schema 校验失败=attempt FAILED+死信，**不再允许只存 digest 的成功路径**（修 D2）
- dead-end 防回归测试：reviewer DONE 后断言 finding 表有该 attempt 来源行或显式零处置记录

## 5. Leader 消费契约

```
Leader 输入 = {
  precheck_findings: mu.agent_finding WHERE source='precheck',
  reviewer_findings: mu.agent_finding WHERE source='reviewer'（含 AT-* 新增）,
  dispositions: orchestration_decision(stage='review_disposition'),
  protection: pull_request.branch_protection_status（落库后——修 D6）
}
输出 = {
  stage='leader_decision_after_review',
  decision ∈ {fix_required, clean_complete, review_blocked(新：仅 reviewer 失败), no_merge_reviewed(新：protection unknown)}
  payload.finding_dispositions: [{rule_id, adopted: bool, source_of_truth: 'precheck'|'reviewer', conflict_resolution: 'reviewer_wins'|'severity_max'|'manual'}]
}
冲突规则（默认，Beta）：reviewer 驳回 precheck 的 P3/P2 → 采纳驳回；驳回 P0/P1 → 保留双记录+升级人工（不静默丢弃任何一方）
```

## 6. 威胁模型（Reviewer 代码上下文出站）

| 威胁 | 缓解 |
|---|---|
| redaction 漏检 → secret 到 Provider | 双通道 redaction（正则+熵）+ 出站前哨兵扫描（合成 secret 注入测试）+ Provider 承诺零留存（条款）+ 泄漏应急：run 出站 digest 可追溯 |
| Provider 注入（模型输出含恶意指令） | 既有 schema allowlist+AT_FORBIDDEN_CONTENT 检查（fixer 已有，reviewer 同用）|
| 恶意 PR 构造大 payload | 24KB 总限+每文件 200 行+二进制排除（复用 EGRESS）|
| 跨租户片段串线 | 片段绑定 run_id+tenant_id；出站日志按租户脱敏计数 |
| 历史出站不可审计 | mu.agent_attempt.evidence_ref 存出站 digest+bytes；审计事件 AT_CONTEXT_EGRESS{files,bytes,redactions_applied} |

## 7. 成本估算（vs 现状）

| 项 | 现状（4 轮） | 目标（4 轮，B 方案） |
|---|---|---|
| LLM 调用/run | 4（rev+lead+fix+verify） | 4（reviewer 变重，leader advisory 保留） |
| 出站 payload | ~1KB（brief） | ≤24KB（brief+片段） |
| 延迟 | ~21s | ~24-28s（reviewer +3-7s） |
| 删除项 | — | 第一层独立 LLM 通道（-1 次调用当开关开时） |
| 净变化 | — | 默认配置下持平或略降（独立通道默认关） |

## 8. 删除/重定义清单（对齐第五节）

删除：第一层独立 LLM Reviewer/建议通道（mu.agent_policy llm_assist 语义+前端开关）；deterministic 的 agent_role='reviewer' 记录（→stage='precheck'）；dead-end output_digest 成功路径。
保留并修复：AgentTeams Reviewer 角色；Reviewer→Leader 工作流；Reviewer findings 持久化+决策输入。

## 9. PR 拆分（修订）

- **PR A（后端核心）**：agent_finding+source 列；precheck 化（stage、新状态 PRECHECK_*）；Reviewer code_context 管道（redaction+EGRESS 复用）；persistReviewerFindings 强制路径；Leader dispositions 契约；protection 落库；REVIEWED_NO_MERGE 状态；删独立 LLM 通道；flag MU_REVIEW_ARCH=v2
- **PR B（前端）**：阶段分组（预检/正式审查/修复/验证）；source 标签（预检 vs Reviewer）；dispositions 展示；policy 面板删 llm_assist；门禁结论与建议链分列
- **PR C（兼容与迁移测试）**：历史 deterministic reviewer 行 → 前端"历史预检"标签（不重写）；15+ 项测试矩阵（新增：redaction 哨兵、reviewer 新增 finding→leader 采用、reviewer 驳回 P0 冲突升级、dead-end 防回归、出站审计事件）

依赖 A→B→C；flag 回滚=v1 全行为。

## 10. 正式 Decision Record（2026-10-02 安全决策门落盘）

```
boundary: REJECT        — 红线维持"代码不出站"，不修订
provider: NOT_CONFIRMED — DeepSeek 无可核验零留存/DPA/地域/删除机制
input_mode: A           — Reviewer 仅接收脱敏风险证据（立即生效）
conflict_policy: ACCEPT — 五条冲突规则全采纳
```

1. **input_mode=A 已批准并立即生效。** AgentTeams Reviewer = 风险证据审查者（evidence reviewer），**不是代码审查者**；不得宣称代码级 AI Review。
2. **B 挂起**：前提（零留存/不训练/DPA/地域/子处理方/保留期/删除机制可核验）全部不可核验——[DeepSeek Privacy Policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html) 仅"as long as necessary"。条件满足后**重跑安全决策门**方可启用。
3. **C 暂不可用**：Beta 无本地可信模型基础设施。
4. **Reviewer 输入允许集**（封闭枚举）：rule_id、severity、文件路径受控表示、已脱敏摘要、precheck 证据摘要、非代码运行元数据。
5. **Reviewer 输入禁止集**：diff、patch、代码行、文件正文、secret/token、未清洗 commit message、日志原文。
6. **冲突规则**（§5 已载）：P2/P3 reject=双记录+可审计降级；P0/P1 reject=冲突+升级人工+禁自动可合并；Reviewer PASS 不覆盖 P0/P1 冲突/protection 阻断/precheck 原始证据；任何冲突不自动生成可合并结论。
7. **REVIEWED_NO_MERGE 语义**：protection unknown 时审查与建议链照常执行，终态=REVIEWED_NO_MERGE，无 mergeable/approved 语义。
8. **r1 撤回确认**：r1 方案 C"删除 AgentTeams Reviewer"的结论已正式撤回。

## 10.x（原待确认项——已被上表裁决取代）



1. **边界修订**："代码不出站" → "未脱敏代码不出站"（B 方案前提，涉及安全红线表述）
2. Provider 零留存条款（DeepSeek 或未来 Provider 的数据处理协议确认）
3. 冲突默认规则（reviewer_wins for P2/P3 + 双记录升级 for P0/P1）是否接受
