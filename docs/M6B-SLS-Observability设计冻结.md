# M6-B · SLS 可观测性设计冻结

> 状态：**本地 SLS 垂直闭环已完成**（fake SLS receiver verified, 79 tests ×2 stable）。
> 分支：`feat/m6b-sls-observability`（基于 main@27b44dc）
> 更新日期：2026-08-11
> Evidence：`evidence/m6/0b/sls-local-vertical-slice.json`
> 边界：本地 fake SLS receiver（非真实 SLS），真实 SLS 尚未接入。
> 前置：M6-A tag `m6a-otel-local-collector-closed`（57 tests PASS）

---

## 1. 目标

设计 OTLP → 阿里云 SLS（Simple Log Service）的生产级可观测性出口，包括：
- OTLP span → SLS Trace 数据映射
- 批量、重试、超时、背压和丢弃策略
- 凭据注入与轮换
- 索引、保留期和成本边界
- 本地 fake SLS contract receiver（禁止接入真实 SLS）

**本轮不接入真实 SLS、Nacos、RocketMQ。**

---

## 2. OTLP → SLS 字段映射（冻结）

| OTLP span 字段 | SLS Trace 字段 | 说明 |
|---|---|---|
| `traceId` | `trace_id` | 32-hex，W3C 标准 |
| `spanId` | `span_id` | 16-hex |
| `parentSpanId` | `parent_span_id` | 16-hex 或空 |
| `name` | `operation_name` | span 名称 |
| `status.code` (1=OK, 2=ERROR) | `status_code` | 整数 |
| `startTimeUnixNano` | `start_time_ms` | 毫秒（纳秒/1e6） |
| `endTimeUnixNano` | `end_time_ms` | 毫秒 |
| `attributes["mp.run_id"]` | `tags.run_id` | SLS tag |
| `attributes["mp.trace_id"]` | `tags.mp_trace_id` | MergePilot 内部 trace_id |
| `attributes["mp.agent_role"]` | `tags.agent_role` | reviewer/fixer/verifier/coordinator |
| `attributes["mp.skill_name"]` | `tags.skill_name` | Skill 名称 |
| `attributes["mp.skill_version"]` | `tags.skill_version` | |
| `attributes["mp.stage"]` | `tags.stage` | review/fix/verify |
| `attributes["mp.attempt"]` | `tags.attempt` | 整数 |
| `attributes["mp.decision"]` | `tags.policy_decision` | ALLOW/DENY |
| `attributes["mp.tool"]` | `tags.gateway_tool` | MCP 工具名 |
| `attributes["mp.correlation_id"]` | `tags.correlation_id` | |
| `attributes["mp.duration_ms"]` | `duration_ms` | 计算字段 |
| `attributes["mp.final_status"]` | `tags.final_status` | OK/ERROR/DENIED/TIMEOUT |
| resource `service.name` | `service_name` | 固定 "mergepilot" |

### 禁止映射的字段（脱敏 denylist 复用 M6-A）

以下字段**永不**出现在 SLS payload 中（与 M6-A `redact_attributes` 一致）：
- `token`, `secret`, `password`, `pat`, `api_key`, `authorization`, `cookie`, `session`
- 值匹配 `ghp_`, `ghs_`, `sk-`, `AKIA`, `xox`, `BEGIN * PRIVATE KEY`

---

## 3. 批量与导出策略（冻结）

### 3.1 批量

| 参数 | 默认值 | 说明 |
|---|---|---|
| `batch_max_size` | 64 | 每批最大 span 数 |
| `batch_timeout_ms` | 2000 | 达到超时即使未满也发送 |
| `batch_max_bytes` | 1 MiB | 批次 JSON 序列化上限 |

### 3.2 重试

| 参数 | 默认值 | 说明 |
|---|---|---|
| `retry_max_attempts` | 3 | 最大重试次数（含首次） |
| `retry_base_delay_ms` | 500 | 指数退避基数 |
| `retry_max_delay_ms` | 5000 | 退避上限 |
| `retry_on_status` | [500, 502, 503, 504, 429] | 触发重试的 HTTP 状态码 |

### 3.3 超时

| 参数 | 默认值 | 说明 |
|---|---|---|
| `export_timeout_ms` | 2000 | 单次 HTTP POST 超时 |
| `total_export_budget_ms` | 6000 | 一批 span 的总导出预算（含重试） |

### 3.4 背压与丢弃

- 当 `batch_queue_size > queue_max_size (default 256)` 时，**丢弃最旧未发送的 span 批次**（drop-oldest），并记录 `span_drop_count`。
- 丢弃是静默的（不抛异常），但 `dropped_batches` 计数器递增。
- **核心业务状态机从不阻塞**：所有导出操作在后台线程进行，主线程只入队。

### 3.5 Fail-closed

- SLS 不可达时：span 进入队列 → 批量超时 → 重试耗尽 → 丢弃（drop-oldest if queue full）。
- **不降级为 passthrough**（不绕过 OTel 直接写日志）。
- 业务逻辑的返回值和状态机转换**绝不依赖** SLS 导出成功。

---

## 4. 凭据注入与轮换

### 4.1 凭据来源

| 凭据 | 来源 | 说明 |
|---|---|---|
| SLS endpoint | `SLS_ENDPOINT` env | `https://{project}.{region}.sls.aliyuncs.com` |
| SLS access key ID | `SLS_ACCESS_KEY_ID` env | 阿里云 RAM AK |
| SLS access key secret | `SLS_ACCESS_KEY_SECRET` env | 阿里云 RAM SK |
| SLS project | `SLS_PROJECT` env | SLS 项目名 |
| SLS logstore | `SLS_LOGSTORE` env | Trace logstore |

### 4.2 轮换

- 凭据从 env 读取，不在代码中硬编码。
- 支持**运行时热轮换**：`SLS_CREDENTIAL_FILE` 指向一个 JSON 文件，exporter 每 60s 重新读取。
- 凭据值**永不**出现在 span 属性、日志或 SLS payload 中。

### 4.3 最小权限

- RAM 角色仅需 `log:PostLogStoreLogs` 权限。
- 不需要读、删除或管理权限。

---

## 5. 索引、保留期与成本

### 5.1 索引

| SLS 字段 | 索引类型 | 用途 |
|---|---|---|
| `trace_id` | keyword | 精确查询 |
| `run_id` | keyword | 按 run 过滤 |
| `agent_role` | keyword | 按角色过滤 |
| `operation_name` | text | 按 span 名搜索 |
| `status_code` | long | 按状态过滤 |
| `duration_ms` | long | 按耗时排序 |
| `start_time_ms` | long | 时间范围查询 |

### 5.2 保留期

| 数据类型 | 保留期 | 说明 |
|---|---|---|
| Trace spans | 7 天 | 复赛期间足够 |
| Error spans | 30 天 | 标记 `status_code=2` 的额外保留 |

### 5.3 成本边界

- 预估每 PR 约 20-50 spans（6 Skill × 3-5 Gateway calls）。
- 日均 PR 量 10 → 日均 spans 200-500 → 月均 ~15K spans。
- 每 span ~500 bytes JSON → 月均 ~7.5 MB。
- **远低于 SLS 免费额度**（每月 500MB 写入）。

---

## 6. 本地 Fake SLS / Contract Receiver

### 6.1 用途

- 验证 OTLP → SLS 映射正确性（字段名、类型、脱敏）。
- 验证批量、重试、超时、背压、丢弃行为。
- **禁止接入真实 SLS**。

### 6.2 实现

- `tools/otel/sls_exporter.py`：SLS-format exporter（HTTP POST JSON）。
- `tools/otel/fake_sls_receiver.py`：本地 HTTP receiver，解析 SLS JSON，验证 schema。
- 批量队列 + 后台导出线程。
- 测试覆盖：schema 校验、脱敏、重试、超时、背压丢弃、fail-closed。

---

## 7. 测试覆盖

| 类别 | 测试 |
|---|---|
| Schema | SLS JSON 字段名/类型正确；必填字段存在 |
| 脱敏 | PAT/token/secret → `<redacted>` 在 SLS payload 中 |
| 重试 | 500/502/503 → 重试 3 次；200 → 不重试 |
| 超时 | 慢 receiver → 2s 超时；总预算 6s |
| 背压 | 队列满 → drop-oldest；dropped_batches 计数 |
| Fail-closed | SLS 不可达 → 业务正常返回 |
| 批量 | 64 spans → 1 批；65 spans → 2 批 |
| 凭据 | env 注入；不在 payload 中；热轮换 |
| 向后兼容 | M6-A 57 项测试全部通过 |

---

## 8. 不在本轮范围

- 真实 SLS 接入（需生产凭据 + operator 授权）
- Nacos / RocketMQ
- 生产 HiClaw trace 采集
- Metrics（仅 Trace）
- 告警规则
