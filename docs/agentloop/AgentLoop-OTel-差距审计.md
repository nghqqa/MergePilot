# AgentLoop 接入前 · 现有 OTel 能力差距审计（PoC Phase 2）

> 日期：2026-08-27　分支：`feat/agentloop-otel-genai-poc`  
> 方法：完整读取 `tools/otel/otel_spans.py`（582 行）并 grep 全部运行时调用点；对照 M6A/M6B 设计冻结文档。

## 十问十答

**Q1 当前有哪些 Span？**
四类冻结合同（`tools/otel/otel_spans.py:361-419`）：`controller.process_event`（实挂于 `m4f_controller.py:366`）、`gateway.call_tool`（`gateway_client.py:257`，仅当调用方传 run_id/trace_id 时创建）、`skill.<name>`（运行时侧 `skills/common/runtime/cli.py:410` 与 worker 侧 `m4f_skill_worker.py:312` 双挂点）、`mcp.upstream`（合同已冻结）。另有 RAG 扩展 span：`rag.query/rag.result/rag.fallback`（`tools/rag/rag_retrieval_service.py:486/587/604`）。**没有** Entry、Agent、Step、LLM 四类 PoC 目标 span。

**Q2 当前 HTTP traceparent 如何传播？**
序列化/解析库函数完整实现且 fail-closed（`otel_spans.py:438-499`：W3C 格式、非法输入返回 None）。**但全仓没有任何生产调用点**：`_call_tool`（MCP HTTP）与 `matrix_request` 均不注入 header；docstring 声称的"GATEWAY header injection"（`gateway_client.py:253`）实际未接线。结论：能力已备、链路未通。

**Q3 Matrix 消息是否携带 Trace Context？**
否。派单模板为纯文本（`STAGE_TPL`，workflow-controller `controller.py:1039-1041`），正文只有产物路径约定；完成识别靠正则 `PAT_COMPLETE/PAT_SUBMIT`。`mp.trace_id` 只进审计库与 envelope JSON，任务房间的消息体断链。

**Q4 是否符合 OTel GenAI 语义？**
不符合。无任何 `gen_ai.*` 属性；`mp.agent_role` 是普通属性而非 Agent 型 span；model/token 类属性无数据源。**边界事实**：LLM/ReAct 循环发生在 HiClaw 运行时内，本仓库代码不可达（grep 无任何模型 SDK 依赖）——本轮无法在不伪造的前提下产生 LLM/Step span，设计需如实处理。

**Q5 是否已有单一 TracerProvider？**
本工程是自研轻量方案（非官方 SDK）：全局唯一 `_global_collector` 单例（`set_collector`，`otel_spans.py:228`），天然满足"单一出口"。但**生产运行时从未初始化任何 collector/exporter**（`DualCollector/OTLPExporter` 仅出现在测试里），当前真实运行的 span 产生后被静默丢弃。若后续引入官方 SDK/LoongSuite，必须沿用"进程内只允许一个 provider/一个 exporter 初始化点"约束。

**Q6 exporter 失败如何处理？**
三层 fail-closed：`start_span` 收尾吞异常（348-352）、`DualCollector.add_span` 吞异常（576-582）、`OTLPExporter.export` 计数 `self._failed` 且不重试（522-545）。业务永不被观测阻断 ✔。缺口：失败计数是实例私有字段，**没有可对外读取的 export-failure 统计**，不符合"必须生成本地 telemetry export failure 计数"要求。

**Q7 Prompt/Response 是否被采集？**
事实上未采集（现有调用点只传 trace_id/run_id 元数据），但属于"没人传"而非"机制禁止"——无内容采集开关与守卫断言。PoC 需求：默认关闭要有机器可验证的保证。

**Q8 当前脱敏规则？**
键模式 12 条 + 值模式 6 条（`otel_spans.py:53-62`），在 `SpanRecord` 构造、`set_attribute`、`add_event` 三处强制 `redact_attributes`（命中即 `<redacted>`）；exception event 只留类型+截断 200 字符消息、不带 traceback（338-344）。缺口：GitHub PAT 家族有覆盖（ghp_/ghs_/gho_）但**缺阿里云系凭证形态（LTAI/AKID 等）与 `Bearer ` 前缀形态**；"脱敏过程自身失败→丢弃该 span 导出"的行为不存在（现为尽力而为）。

**Q9 跨容器传播缺口？**
三条：(a) Matrix 消息体无载体（见 Q3）；(b) HTTP 注入未接线（见 Q2）；(c) Resource 维度未映射——M6A 冻结文档规划的 Docker labels `com.mergepilot.* → OTel Resource 属性` 未实现，`OTLPExporter._span_to_otlp` 写死 `service.name=mergepilot`，`run_id/scope/agent` label 全部丢失。同一 run 的多进程 span 仅靠 envelope/task 文件透传的字符串 `trace_id` 保持同 Trace，父子关系跨进程必然断裂。

**Q10 AgentLoop 接入的最小修改文件范围。**
1. `tools/otel/otel_spans.py`：SpanRecord 增加 `links`；`start_span` 支持 `parent_span_id/links` 跨进程归位；新增 GenAI span builder（entry/agent/stage），属性走白名单；值脱敏补充 LTAI/Bearer 形态；redaction 失败→DROP 信号；模块级 export 统计 `get_export_stats()`。
2. 新增 `tools/otel/exporter_init.py`：仅从环境变量读取出口配置，构造 memory+OTLP 双写 collector（默认关闭，保持现状）。
3. `tools/workflow-controller/controller.py`：run 入口建 Entry span；派单时在 STAGE_TPL 尾部附结构化 trace 行（carrier 进消息体，不动鉴权面）；收到 TASK_COMPLETED 时解析 carrier 以 Link 接续并关闭对应 Agent 窗口 span；stage 状态转换记为 span event。
4. `tests/otel/test_agentloop_poc.py`：≥20 个用例（层级、round-trip、carrier 序列化恢复、非法 carrier fail-closed、脱敏新形态、failure 计数、单一 collector、回归）。
不触碰：Gateway 权限语义、SOUL 合同、Skill 输出 contract、审计库 schema。Full-stack 四 Agent 房间内的真实接线还依赖 HiClaw worker 转发 carrier（本轮标记 NEEDS_RUNTIME_WIRING）。

## 结论

骨架质量高（冻结合同、fail-closed、脱敏意识、双出口预留），核心缺口集中于三点：**未接线**（HTTP/Matrix/Resource）、**未启用**（生产无 collector）、**不合 GenAI**（无语义属性）。三项均可增量修复，不需要重构，M6A 冻结合同全部保留。
