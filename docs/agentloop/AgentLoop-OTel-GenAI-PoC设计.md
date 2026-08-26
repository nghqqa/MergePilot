# AgentLoop · OTel GenAI Trace PoC 设计（冻结稿）

> 日期：2026-08-27　分支：`feat/agentloop-otel-genai-poc`　状态：DESIGN_FROZEN（离线可测部分）
> 前置：`AgentLoop-环境前置核验.md`（当前 BLOCKED_CONFIGURATION）、`AgentLoop-OTel-差距审计.md`

## 0. 技术路线裁定

在现有自研轻量 OTel 栈（`tools/otel/otel_spans.py`）上做**增量扩展**，不引入官方 Python SDK 依赖：

- 唯一出口原则：进程内只允许一个 collector 初始化点（新文件 `tools/otel/exporter_init.py`），出口地址只来自环境变量。AgentLoop 以 OTLP/HTTP receiver 形态接入——启用与否、指向哪里，全部由环境变量决定；
- LoongSuite 自动探针与本方案不冲突：探针属外挂进程库；本工程进程内永不初始化第二套 exporter，消解双链路冲突；
- M6A/M6B 冻结合同（四类 span 名、denylist、ID 体系）原样保留，GenAI 是新增层不是替换层；
- 审计数据库仍是治理证据的唯一事实源，AgentLoop Trace 只是只读镜像。

## 1. 目标调用树（PoC 实际可产出的诚实版本）

```text
Entry: mergepilot.pr_review                    ← Controller 在 run 起始创建，终止态关闭
├─ Agent: mergepilot.agent.manager             ← 派发监视窗口（Controller 进程内）
│  ├─ Stage: mergepilot.stage.review           ← 窗口型 span：dispatch→completed
│  │  ├─ Link → 生产端末span（Matrix 异步交接）
│  │  ├─ Tool: gateway.call_tool[mp.tool=sast_scan]   （既有冻结名，跨 hop 同 trace）
│  │  └─ Tool: skill.sast_scan                        （运行时侧既有挂点）
│  ├─ Stage: mergepilot.stage.fix               …同构（pr_lifecycle 等）
│  └─ Stage: mergepilot.stage.verify            …同构（test_runner 等）
```

连通语义：**hop 内严格父子**（thread-local 链，现状保留）；**hop 间用 traceparent 等值 + Span Link 表达异步接续**，消费方 span 的父字段为空但带 `link(trace_id, producer_span_id)`，由完整性检查器识别为合法接力。这是"Span Link 或官方推荐等价方式"中明确选择的等价方式。

**本轮明确不产生的 span（防伪造红线）：**
- `LLM` span：模型调用发生在 HiClaw 运行时内部，本轮无数据源，不创建任何 LLM span（含 synthetic 流程）。待 HiClaw 侧挂钩后另立工作包，标 NEEDS_RUNTIME_WIRING；
- `ReAct Step` span：同理（Step 循环在 Agent 进程内），以 Stage 窗口替代并注明语义差异；
- `Retrieval/Rerank/Memory` span：RAG 未接入本轮，禁止生成成功的 Retrieval span；
- `mcp.upstream`、`rag.*` 仅在真实发生时出现。

Phase 6 最小健康检查树独立于业务链：

```text
Entry: mergepilot.poc.health_check
└─ Tool: tool.synthetic_health_check      （本地合成动作，不触网、无上游依赖）
```

## 2. Agent 窗口 span 与 Matrix 载体

- **窗口语义**：Agent/Stage span 由 Controller 从派单事件起保持打开，收到该 `(run,stage)` 的 `TASK_COMPLETED` 后落状态关闭（OK/ERROR）。manager 窗口覆盖全 run。
- **载体（carrier）进消息体**：派单文本尾部追加单行结构化 trailer：

  ```text
  [MPTRACE] v=1 tp=00-<32hex>-<16hex>-01 run=<run_id>
  ```

  选择消息体而非 header：不动 `matrix_request` 鉴权面；无法解析它的旧消费方把它当无害文本忽略（向后兼容）；正则严格校验，非法即整体忽略（fail-closed，禁止传播破损上下文）。trailer 只含十六进制 ID 与 run_id，不属于敏感信息。

## 3. 属性白名单（builder 强制执行）

仅允许以下键进入 GenAI 族 span，其余一律丢弃：

`run_id, task_id, agent_role, stage, attempt, tool_name, tool_status, policy_decision, finding_count, final_decision, verification_status, human_intervention, model_provider, model_name, token_usage, duration_ms`

映射到 span 属性命名空间：业务字段冠以 `mp.`（沿用现合同），预留 `gen_ai.*` 字段位但不虚构取值（model_provider/model_name/token_usage 本轮恒缺省——数据源在 HiClaw 内）。在此白名单之上继续叠加既有 `redact_attributes` 双保险。

## 4. 禁止采集清单（超集继承现有 denylist）

现有键模式 12 条之外，值模式补充阿里云系与 header 形态：`LTAI`、`AKID`、`Bearer `、`sk-ant`。以下内容无论以何属性名出现都不得导出：
PAT、AccessKey、License Key、Authorization Header、数据库 DSN、Cookie、Prompt 全文、Response 全文、代码全文、Diff 全文、Matrix access token、用户目录与机器绝对路径。
**脱敏失败处理升级**：redaction 过程自身抛错时，不再"尽力而为"，标记该 span 为 drop、计入 `dropped_redaction` 统计、不进入任何出口。

## 5. 错误处理与治理规则

1. 可观测出口不可用 → 业务主流程零影响（延续三层 fail-closed），同时维护模块级计数器 `get_export_stats()`：`sent / failed_export / dropped_redaction / duplicated_export_guard`；
2. 出口配置只来自环境变量：`MP_OTEL_EXPORT_ENABLED`（默认 0＝行为与现状完全一致）、`MP_OTLP_ENDPOINT`（默认 `http://127.0.0.1:4318/v1/traces`）、`MP_SERVICE_NAME`（默认 `mergepilot`）；
3. 重复上报防护：`exporter_init.ensure_initialized()` 幂等，二次调用返回同一实例（单一 Provider/Exporter 断言有测试锁定）；
4. Trace 无秘密断言成为 CI 门禁：对所有导出属性跑 denylist 扫描，任一命中即测试失败。

## 6. 完整性与验收口径（供 Phase 9 门禁复用）

- **Trace 完整率 100% 的判定**：同一 `trace_id` 下每个 span 满足其一——(a) 父 span 同 trace 可解析；(b) 是合法 hop 根且携带入站 link；(c) 是 Entry。
- **AgentLoop 可见性**（联网阶段）：service.name 正确、父子关系正确、attributes 按 §3 白名单可见、无 §4 内容、单实例无双报。
- **本地↔云对齐**：audit 库事件与 AgentLoop span 以 `run_id` 关联（`mp.run_id`）。

## 7. 最小修改范围（与审计 Q10 一致）

| 文件 | 动作 |
|---|---|
| `tools/otel/otel_spans.py` | links 支持、跨进程归位参数、GenAI builder、脱敏增补与 drop 语义、export 统计、carrier 编解码 |
| `tools/otel/exporter_init.py` | 新建：env-only 初始化 + 幂等单例 |
| `tools/otel/poc_health_check.py` | 新建：Phase 6/9 通用的最小 trace 发射器（entry+tool 两段，支持开/关对照） |
| `tools/workflow-controller/controller.py` | run 入口 Entry、派单附 trailer、完成解析以 Link 接续并关窗、stage 转换记 span event |
| `tests/otel/test_agentloop_poc.py` | 新建：≥20 用例 |

**不触碰**：Gateway 权限语义、SOUL 合同、Skill 输出 contract（含 envelope JSON schema——skill 进程不改）、`matrix_request` 鉴权路径、审计库 schema。
**NEEDS_RUNTIME_WIRING**：HiClaw worker 房间内把 carrier 透传到 Skill envelope 属运行时改造，本轮只在测试中以仿真消费方验证协议正确性。

## 8. 实施范围修订（2026-08-27，实现日补充）

实现中把 controller 接线拆为两步走，以"不改状态机行为"为最高优先：

- **本轮已落地**：`send_mention` 出口注入 MPTRACE trailer；TASK_COMPLETED(review/fix) 摄入侧解析载体并以 Link 接续产生 `agent.handoff_complete` span（pg 提交后、失败静默）；Entry/Agent 全窗口生命周期挂接未在本轮接入（需要跨多个终态路径管理窗口开闭，留给运行时联调阶段一次完成），其库层构件（entry_span / AgentWindowSpan）已就绪并有测试锁定；
- **测试中的仿真边界**：四房间真实消费方是 HiClaw worker，属 NEEDS_RUNTIME_WIRING，测试以内存仿真验证协议（append↔parse 往返、非法拒收）。
