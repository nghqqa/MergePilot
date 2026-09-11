# Phase 14.2H-WD-AGENTLOOP-OTEL — AgentLoop/OTel GenAI Trace（证据回放模式）

## 裁决

**AGENTLOOP_OTEL_TRACE_READY_FOR_DEMO**（verdict.json）

## 这是什么

为已完成的 **PR #1（普通协同）** 与 **PR #2（高危安全门）** 两个真实案例生成的
**统一 AgentLoop/OTel GenAI Trace**：trace-schema 对齐 OTel span/GenAI 语义约定
（`gen_ai.*`）并扩展 `agentteams.*` 属性；支持按 project / task / agent / run 查询；
span 以 parent_span_id 构成父子树；LLM、工具、Matrix、人工审批四类事件用
`span_kind` 区分着色。

## 诚实性声明（关键）

- `mode = "evidence-replay"`：两个案例**已经完成**，且约束禁止发起新任务/重试，
  因此 trace 由**不可变证据**确定性重建——原始 Matrix 事件（只读 API 拉取，281 条
  消息扫描）、任务存储 meta（assigned_at/acknowledged_at/submitted_at）、阶段日志、
  人工门批准记录。span 时间戳即**原始事件时间**，非采集时间；每条 span 附
  `provenance_refs` 与 `time_certainty`。不存在把历史日志伪装成实时 Trace 的行为。
- 未修改 AgentTeams/CoPaw 业务逻辑、OpenClaw 主线、PR #1/PR #2；未发送 kickoff/
  retry/新任务消息；未调用 delegate_task；project/DAG/task 状态零变更。

## 文件清单

| 文件 | 说明 |
|---|---|
| trace-schema.json | Trace 模型 JSON Schema（span 种类/属性/脱敏/溯源） |
| pr1-result-trace.json | PR #1 结果 trace（内嵌 result_evaluation，9 spans） |
| pr1-trajectory-evaluation.json | PR #1 轨迹评估（7 检查项全 PASS） |
| pr2-result-trace.json | PR #2 结果 trace（内嵌 result_evaluation，18 spans） |
| pr2-trajectory-evaluation.json | PR #2 轨迹评估（8 检查项全 PASS，含门/事故/恢复） |
| human-gate-trace.json | 人工门真实状态转换（request→blocking→approval）+ 门前后对比 + 阻塞不变量证明 |
| event-correlation.json | Matrix 事件 ↔ 任务 meta ↔ span 三向关联（281 行，脱敏） |
| redaction-audit.json | 脱敏规则与自检（token 未出现在输出 = False→Clean） |
| demo-console-trace-contract.json | 演示控制台渲染合同（查询/着色/EVIDENCE REPLAY 徽章） |
| stability-180s.json | 180 秒三轮观测：canonical hash 完全一致 |
| verdict.json | 裁决 |
| SHA256SUMS | 全文件校验 |

## Trace 覆盖（对照任务要求）

| 要求项 | 落点 |
|---|---|
| project/session/run | `agentloop.project` / `agent.session.run`（run_id） |
| Agent 身份与角色 | `agentteams.agent / agent_role` |
| LLM 调用 | `GENAI_LLM_SESSION`（gen_ai.operation.name / request.model） |
| 工具调用 | `GENAI_TOOL_CALL`（taskflow/message/filesync，arguments_hash） |
| projectflow(ready_nodes) / taskflow(delegate_task) | 计划/委派 span（delegate_task 均带 matrix_event_id） |
| Reviewer/Fixer/Verifier 状态转换 | task.submit / task.state_transition（from→to） |
| 人工门请求/批准 | `human_gate.request` → `blocking_window` → `approval`（**真实状态转换**，非静态说明；阻塞不变量给出"窗口内无派发事件"的缺席证明） |
| 重试与异常 | `INCIDENT` spans：tool_guard 会话清空 ×2（recovered）、MinIO 树清空（recovered） |
| 最终任务结果 / project completed | `task.submit` + `project.completed`（PR #2 标注 merge 禁止、PR OPEN） |

## 双重评估

- **结果评估**（result_evaluation，内嵌于 result-trace）：Reviewer 是否识别高危 /
  Fixer 是否修复 / Verifier 是否通过 / 项目状态是否正确完成 —— 两案例全 PASS。
- **轨迹评估**（trajectory-evaluation 文件）：正确工具、无重复调用、无越门、失败恢复、
  无错误委派、无无效重试、工具顺序符合 DAG、批准前保持阻塞 —— PR #1 7 项、PR #2 8 项全 PASS。
- 两者物理分离：不同字段块 / 不同文件，互不引用结论。

## 脱敏与秘密

- 消息/代码内容仅保留 `sha256[:12]` + 长度 + 40 字符头；字段名保留
- Matrix accessToken 仅用于容器内只读 GET，从未序列化进任何输出（自检 False）
- 扫描规则与结果见 redaction-audit.json 与 SECRETS-SCAN.txt（真实秘密 0 命中）

## 复现

在 manager 容器内：`/opt/venv/standard/bin/python /tmp/agentloop_collector.py`
（源码 artifacts/agentloop_collector.py）。多次运行 canonical hash 恒定
（stability-180s.json：t0/+90s/+180s 三轮一致）。
