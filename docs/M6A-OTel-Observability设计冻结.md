# M6-A · OTel 可观测性设计冻结

> 状态：**本地 OTLP 闭环已完成**（local collector verified, 57 tests ×2 stable）。
> 分支：`feat/m6a-otel-observability`（基于 main@b214518）
> 更新日期：2026-08-11
> Evidence：`evidence/m6/0a/otel-local-collector.json`
> 上游：AgentTeams v1.2.2（commit 849182a），D2B-3 PASSED（hiclaw_live=true）

---

## 1. 现有相关性基础设施审计

MergePilot 已有成熟的相关性 ID 骨干，但零 OTel SDK 代码：

| 组件 | 现有 ID | 传播方式 | OTel 注入点 |
|---|---|---|---|
| M4F Controller (`m4f_controller.py`) | `run_id`, `trace_id`, observer 事件流 | `_emit(observer, event, run_id, trace_id, **fields)` | **最佳**：observer 直接映射为 span 事件 |
| Policy Gateway (`gateway.py`) | `correlation_id`（UUID），`phase`（INTENT/RESULT/ERROR） | `mcp_calls` INSERT-only 审计表 | 每次调用 = 一个 `gateway.call_tool` span |
| Skill Runtime (`skills/common/runtime/`) | `request_id`, `trace_id`（envelope 契约） | 请求/响应 envelope | `_execute` 包裹为 `skill.<name>` span |
| Skill Worker (`m4f_skill_worker.py`) | `job_id`, `invocation_id`, `trace_id` 完整性校验 | `skill_job_outbox` 表 | Job 执行 = `skill.<name>` span |
| PostgreSQL Audit (`m3_state.sql` 等) | `run_id`, `trace_id`, `correlation_id`, `idempotency_key` | 外键链 | 镜像为 span 属性 |
| Docker Labels (`com.mergepilot.*`) | `run_id`, `scope`, `agent`, `hardened` | 容器标签 | 直接映射为 OTel Resource 属性 |

**结论**：不需要替换现有 ID 体系；OTel trace_id/span_id 作为**新增层**叠加，MergePilot 内部 ID 作为 span 属性保留。

---

## 2. OTel Span 命名契约（冻结）

| Span 名称 | 触发点 | 必需属性 |
|---|---|---|
| `controller.process_event` | Controller 顶层事件处理 | `mp.run_id`, `mp.trace_id`, `mp.agent_role`, `mp.stage`, `mp.attempt` |
| `gateway.call_tool` | 每次经 Policy Gateway 的 MCP 调用 | `mp.run_id`, `mp.trace_id`, `mp.correlation_id`, `mp.tool`, `mp.decision` |
| `skill.<skill_name>` | 每次 Skill 执行 | `mp.run_id`, `mp.trace_id`, `mp.skill_name`, `mp.skill_version`, `mp.request_id` |
| `mcp.upstream` | 每次上游 Docker/GitHub API 调用 | `mp.run_id`, `mp.trace_id`, `mp.correlation_id`, `mp.endpoint`, `mp.method` |

**Span 层级**：
```
controller.process_event (root)
  ├── skill.diff_parse
  ├── gateway.call_tool (reviewer → sast-scan)
  │     └── mcp.upstream (GitHub API read)
  ├── skill.sast_scan
  ├── gateway.call_tool (fixer → create_branch)
  │     └── mcp.upstream (GitHub API write)
  ├── skill.pr_lifecycle
  ├── gateway.call_tool (verifier → merge)
  │     └── mcp.upstream (GitHub API merge)
  └── skill.case_retrieval
```

---

## 3. 敏感字段禁止列表（冻结）

以下键/值模式**永不**写入 span 属性：

**键模式**（大小写不敏感子串匹配）：
`token`, `secret`, `password`, `passwd`, `pat`, `api_key`, `apikey`, `credential`, `private_key`, `auth_token`, `authorization`, `cookie`, `session`

**值模式**（子串匹配）：
`ghp_`, `ghs_`, `gho_`, `sk-`, `AKIA`, `xox`, `BEGIN RSA PRIVATE`, `BEGIN OPENSSH PRIVATE`

匹配的键/值自动替换为 `<redacted>`。Traceback **不**记录（可能包含环境变量中的密钥）。

---

## 4. 上下文传播

- **进程内**：`threading.local()` 携带 `SpanContext`（trace_id, span_id, run_id）
- **跨进程**（Skill subprocess）：通过 envelope 的 `trace_id` 字段传播；子进程从 envelope 恢复 `SpanContext`
- **跨服务**（Gateway HTTP）：通过 HTTP header `X-MP-Trace-Id` / `X-MP-Run-Id` 传播（未来；当前为进程内）
- **OTel trace_id vs MergePilot trace_id**：OTel SDK 生成 32-hex trace_id；MergePilot 的 trace_id 存储为 `mp.trace_id` 属性，用于与 DB 审计表关联

---

## 5. Fail-closed 语义

- OTel 导出失败**不阻断**业务逻辑（observability must not break pipeline）
- 失败时 span 被丢弃（不内联重试）；错误写 stderr
- 异常在 span 中记录类型+消息（不含 traceback），然后 re-raise

---

## 6. 最小垂直闭环

实现路径：Controller → Gateway → Skill → MCP，使用 in-memory collector 验证。

```
controller.process_event (run_id="test-run", stage="review")
  └── skill.diff_parse (skill_name="diff_parse")
  └── gateway.call_tool (tool="pull_request_read", decision="ALLOW")
       └── mcp.upstream (endpoint="/pulls/1", method="GET")
  └── skill.sast_scan (skill_name="sast_scan")
```

---

## 7. 本地 Collector（已完成）

- `InMemoryCollector`：线程安全，收集所有 span 到内存列表
- `LocalOTLPReceiver`：最小 OTLP/HTTP receiver（127.0.0.1:4318/v1/traces），解析 OTLP JSON，存储 span
- `DualCollector`：InMemory + OTLPExporter 双写
- `OTLPExporter`：timeout=2s, fail-closed (不可达 → 静默丢弃)
- 测试已验证：完整 trace 到达 receiver，parent 链正确，mp.* 属性完整
- **未部署官方 otelcol**；当前使用 MergePilot 最小 receiver
- SLS、生产 trace、告警仍未实现

---

## 8. 测试覆盖（4 条路径）

| 路径 | 场景 | 断言 |
|---|---|---|
| 正常 | Controller→Skill→Gateway→MCP 全链 OK | span 数 ≥ 4，全部 status=OK，run_id 一致 |
| 拒绝 | Gateway 返回 DENY | gateway span status=ERROR，skill span 不创建 |
| 超时 | MCP 超时 | mcp span status=ERROR，event=timeout |
| 回滚 | Controller verify-fail → rollback | controller span 有 rollback event，status=OK |

附加测试：
- 脱敏：带 secret 的属性 → `<redacted>`
- 上下文传播：child span 的 trace_id == parent trace_id
- fail-closed：collector 异常不阻断业务

---

## 9. 不在本轮范围

- SLS 后端接入（M6-B）
- W3C traceparent 跨 HTTP 传播
- 真实 otelcol 容器部署
- 生产 HiClaw trace 采集（需 hiclaw_live 运行窗口）
- Nacos/RocketMQ trace 传播
