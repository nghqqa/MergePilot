# 生产能力差距分析：input_mode=A 的 Review 能力边界（ADR-001-r2 补充）

- 日期：2026-10-02；基线 main=`b01a91de84d9`（只读；PR A 已停在 docs-only commit `9f797ad`，零代码实施）
- **最终裁决：EVIDENCE_TRIAGE_ONLY**

---

## 1. AgentTeams Reviewer 实际可见字段（代码实证 @b01a91de）

`sanitizeBrief`（agentteams-executor.mjs:187）→ Matrix 轮 `rp.ask(brief, results)`（fix-orchestrator.mjs:300）：

```
{ untrusted_findings: [ ≤20 条 × {
    rule_id  ≤60 字符,        // 如 "R-SECRET"
    severity,                 // P0-P3
    path     ≤200 字符,       // 文件路径字符串
    line_start,              // 行号
    masked   ≤120 字符        // 已脱敏摘要（如 "sk-***"）
} ] }
```

加一句任务指令（"review {brief}. Produce structured findings."）。**没有其他任何输入**——无 diff、无代码行、无文件正文、无 commit message、无 PR 描述。

## 2. 证明：它无法发现 precheck 之外的新代码问题

形式化论证：
- Reviewer 的输出是其**输入的函数**（LLM 无工具调用、无文件系统访问、无网络——copaw worker 的任务上下文只有 Matrix 消息文本）。
- 输入是 precheck findings 的投影（sanitizeBrief 是 agent_finding 的纯函数，无外部数据源）。
- 因此 Reviewer 的信息集 ⊆ precheck 信息集 + LLM 先验知识。它能做的只有：对既有证据分类（confirm/reject/改级）、格式整理、或**凭先验幻觉**产出无证据支撑的"新发现"——后者不是发现，是编造。
- **结论：任何不在 precheck findings 中的真实代码缺陷，在数学上不可能经由该链路被发现。** "new_findings" 在 input_mode=A 下只能合法表述为"证据一致性/完整性问题"，不能是代码缺陷。

反证：若 Reviewer 真产出了正确的新代码发现，唯一可能是巧合（先验猜测）——不可审计、不可复现，产品上不可依赖。

## 3. deepseek-direct 位置核验（实测）

- worker 容器 openclaw.json：`deepseek-direct.baseUrl = https://api.deepseek.com/v1`，api=openai-completions——**公网 HTTPS 出站到外部 Provider**。
- ctrl 网络为 docker bridge（agentteams-beta_atnet），非本地推理。
- 结论：当前"AgentTeams 审查"的模型调用 100% 发生在 DeepSeek 的外部基础设施上。

## 4. 概念澄清：两条互不相同的红线

| 陈述 | 含义 | 当前状态 |
|---|---|---|
| "代码不出站" | 代码不离开本机部署边界（到任何外部 Provider） | **当前成立**（brief 无代码） |
| "代码进入本地可信模型" | 代码留在本机/内网的可信推理边界内 | **当前不存在该设施** |

input_mode=B 被否决是因为前者（DeepSeek 条款不可信）。**方案 C（本地模型）不违反前者也不需要后者让步——它缺的是基础设施，不是合规性。** 把两者混为一谈会导致错误地认为 C 也被决策门否决——它没有，它只是 Beta 暂不可行。

## 5. 三条生产路径比较

| 维度 | C1：本地/内网模型读全 diff | B：合规 Provider 读受控 diff | D：人工 Review + AT 证据整理 |
|---|---|---|---|
| 能发现 precheck 外新问题 | **能（完整）** | 能（片段内） | 能（人读代码） |
| 违反"代码不出站" | 否（代码留在本机） | 否（红线修订为"未脱敏"后；Provider 条款已合规） | 否 |
| 前置条件 | 本地推理设施（GPU 或内网推理服务） | 合规 Provider（ZDR+DPA+地域/删除条款可核验）+ 红线修订决策门 | 无 |
| 部署成本 | **高**：GPU 服务器或内网 vLLM/Ollama 服务（单卡 24GB 可跑 32B 级），运维负担 | 低（换 Provider 配置） | 零新增 |
| 模型质量 | 中（开源 32B-70B 弱于 DeepSeek） | 高（顶级 API） | 最高（领域专家） |
| 延迟 | 本地 15-60s/审查（视模型） | 8-12s | 人工小时-天级 |
| 审计 | 最强（零出站，全链本地日志） | 中（出站 digest+条款依赖） | 强（人工记录） |
| 回滚 | 平凡（切回 v2-A） | 平庸（同） | 平凡 |
| Beta 时间影响 | **+2-6 周**（硬件采购/推理服务搭建/评测） | +1 周（Provider 尽调+决策门重跑） | 0（立即可用） |

## 6. 推荐路径

**推荐：分阶段——Beta 期间 D（人工+AT 证据整理）→ 正式版 C1（本地模型）。**

- **D（立即，零成本）**：input_mode=A 的 AT Reviewer 定位为"证据复核+整理"（它已经只做这个）；真正的代码审查由人完成，AT 链产出的是结构化证据包供人快速定位。产品诚实：机器做预检与证据分类，人做代码判断。
- **C1（正式版目标）**：本地/内网模型读全 diff。这是唯一"完整代码 Review 能力 + 零出站"的路径，不依赖任何 Provider 条款。成本是基础设施（单张 24GB GPU + vLLM 即可起步），与既定"代码不出站"红线天然一致。
- **不推荐 B 为目标态**：即使找到合规 Provider，代码出站仍依赖第三方条款持续合规——每换一次 Provider 都要重跑决策门，且红线被永久削弱。

**部署成本（C1 起步配置）**：1× GPU 服务器（24GB 显存，如 RTX 4090/A10）+ vLLM/Qwen2.5-32B 级模型 + 内网推理 endpoint（AgentTeams worker 的 deepseek-direct.baseUrl 改指内网）。**延迟预估**：本地 32B 约 15-30s/审查（vs 当前 DeepSeek ~5s）——可接受。**回滚**：baseUrl 指回即回 A。**Beta 时间影响**：+2-6 周。

## 7. 前端禁语（在能力边界解决前立即生效的约束）

不得出现："代码审查通过"、"AgentTeams 已完成代码 Review"、"AI 代码审查"。
允许表述："规则预检 + 风险证据复核完成；未执行代码级 AI 审查"。

## 8. 产品命名（若保留 input_mode=A）

**产品功能名：规则预检与风险证据复核**（Rule Precheck & Evidence Triage）。
显式不等价声明（UI 常驻）："本功能不构成代码级 Review；代码级审查需人工或可信模型路径。"

## 9. 裁决依据

- CODE_REVIEW_CAPABLE：否——§2 已证明输入_mode=A 链路数学上不可能发现新代码问题
- BLOCKED_PENDING_TRUSTED_MODEL：部分成立但过于绝对——D 路径（人工+证据整理）现在就可用且有价值
- **EVIDENCE_TRIAGE_ONLY**：准确——当前与 input_mode=A 下的真实能力就是证据分诊，产品必须如此定位
