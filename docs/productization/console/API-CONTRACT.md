# 控制台 API 契约（API-CONTRACT）

**版本**：console 0.1.0（snapshot 模式）｜ 所有端点只读、仅 GET、默认仅监听 127.0.0.1:4730。

## 通用约定

- **数据模式**：当前所有数据来自锁定证据包 = `snapshot`（真实历史运行，只读，非实时）。
  每个响应带 `data_mode` 字段；`live` 未接入（`/api/health` 如实报告）。
- **空值**：字段缺失一律 `null`，前端渲染"未记录"；**从不猜测或用演示数据补齐**。
- **错误**：`{ "error": { "code": <http status>, "message": "..." } }`；400=非法参数/路径，404=未知 run/文件。
- **三态分离**：`execution`（执行状态）、`review`（审查结论）、`publish`（发布状态）为三个独立对象，
  各带 `source` 指明数据来自包内哪个文件。

## GET /api/health

```json
{ "ok": true, "service": "mergepilot-console", "version": "0.1.0",
  "data_mode": "snapshot", "live": { "configured": false, "note": "..." },
  "evidence_root": "...", "runs": 18, "packs_with_sums": 18, "started_at": "..." }
```

## GET /api/runs

Query：`repo`（包含匹配）、`pr`（精确）、`execution`、`verdict`、`publish`、`q`（run_id/仓库/SHA/PR 包含）、
`limit`（默认 50，≤200）、`offset`。按 `created_at` 降序。

```json
{ "data_mode": "snapshot", "generated_at": "...", "total": 18, "limit": 50, "offset": 0, "items": [ RunRecord ] }
```

### RunRecord 字段与提取优先级（提取失败即 null）

| 字段 | 类型 | 来源优先级 |
|---|---|---|
| `pack_id` | string | 证据目录名（稳定键，详情路由用它） |
| `run_id` | string? | kickoff.json `run` > check-run.json summary `run_id:` > project/result.md `**Run ID**:` > PR-METADATA.md |
| `repo` | "owner/name"? | delivery-ledger `repo` > result.md `**Repo**:` > PR-METADATA.md > 门决策文件 `Head SHA under review:` 行 > check-run html_url |
| `pr_number` / `pr_url` | int? / string? | 同上来源次序；url 由 repo+pr 构造 |
| `pr_title` | string? | PR-METADATA.md `标题：` 行（仅部分包记录；缺失即 null，前端显示 PR #n 不造标题） |
| `head_sha` / `base_sha` | 40hex? | ledger `observed_*_sha` > result.md 反引号 40hex > PR-METADATA.md > kickoff-as-sent.txt > 门决策文件 |
| `trigger` | "webhook"/"matrix"/"unknown" | 有 ledger=webhook；有 kickoff=matrix |
| `execution` | obj | webhook 轮：ledger 的 status/received_at/claimed_at/processed_at/note/delivery_id；matrix 轮：project/meta.json 的 status（`source` 字段标明） |
| `review` | obj | `verdict`：result.md DAG review 节点 > reviewer 任务 result.md `STATUS:` > check-run summary；`severity`/`cwe` 同源；`human_gate`：gate-approval-sent.json mode > result.md > 门决策 md 文件（APPROVED/REJECTED/RECORDED）；`human_gate_source`：门决策来源文件路径 |
| tasks 内 `findings_path` | string? | review 任务目录 findings.md（或 workspace/findings.md） |
| `publish` | obj | check-run.json → `published`（id/conclusion/url/时间）；无 → `not_recorded`（Matrix 轮零 GitHub 写入）或 `processed_no_checkrun_record` |
| `created_at` / `duration_ms` / `duration_human` | — | ledger received→processed；matrix 轮 kickoff→最后任务提交 |
| `has_sums` | bool | 包内有无 SHA256SUMS |

**状态词含义**（前端徽章 title 同文）：`PROCESSED`=投递完成且已记账；`COMPLETED`/`BLOCKED`=项目 meta 终态；
`verdict` FINDING_CONFIRMED/NOT_CONFIRMED = 独立审查结论（与发布、审批互不等同）。

## GET /api/runs/:packId

RunRecord 全字段 +：

- `project`：project_id/title（kickoff.json / project/meta.json）
- `tasks[]`：task_id/role/status/title/assigned_at/acknowledged_at/submitted_at/result_path/spec_path（tasks/*/meta.json）
- `timeline[]`：ledger 三事件 + kickoff + 任务三事件 + 门决策 + check-run 两事件，按 ts 排序；ts 缺失排最后并保留 `source`
- `dag[]`：result.md 的 DAG 节点行原样标记
- `versions`：run_manifest（历史包均 null——早于 81e0045 的 manifest 机制，如实标注）/ model（usage note 正则，会话级口径注明）/ image（README 文本提及，注明 basis）/ skills 聚合（skill-audit.json）/ span_summary（根目录或 agentloop/ 下）
- `rag`：`state` ∈ called（含逐条 calls：tool/result_status/document_count/source_refs/data_mode/latency_ms/ts）/ no_calls / counted_only（仅 span 计数）/ not_called / insufficient_data。**注意口径**：TRACED/SK5 轮为会话累计（四案例共用会话）；WH 轮 skill-audit 已按 run 窗口重切（CORRECTIONS.md）
- `usage`：usage-summary.json windows + note 原文 + `matched_window`（包名规范化后包含匹配窗口键，如 PR2-SK5→pr2sk5；匹配不到则 null 并展示全部窗口）。**金额永不显示**（无价目表不虚构）
- `skill_audit`：skill-audit.json 原始 invocations（WH 轮有）

## GET /api/runs/:packId/evidence

包内全部文件：`items[]: { path, bytes, sums_status: listed|unlisted|no_sums }`。

## GET /api/runs/:packId/evidence/content?path=<rel>

安全约束：path 必须归一化后落在包内（拒绝 `..`、绝对路径、盘符、反斜杠变体——有单测）。
返回 `{ path, bytes, encoding: "utf-8"|"binary", truncated, text?, sums_status, note? }`。
文本视图上限 512KB（超出截断标注）；**HTTP 响应体为 JSON**，前端按纯文本转义渲染（无 HTML 执行面）。

## GET /api/runs/:packId/evidence/download?path=<rel>

同上安全约束；`Content-Type: application/octet-stream` + `Content-Disposition: attachment`。
仅下载，无任何推送/合并语义。

## GET /api/runs/:packId/integrity

按需执行 SHA256SUMS 全量校验（结果按包签名缓存）：
`{ status: verified|mismatch|no_sums, listed, verified, mismatched[], unlisted_count }`。

---

## live 模式预留（未实现）

授权接入后拟增：`data_mode: "live"` 的 `/api/live/deliveries`（服务器 PG github_deliveries 只读）
与 `/api/live/manifests/:runId`（MinIO run-manifest）。字段沿用本契约；live 与 snapshot 并存、各自标注，不互相冒充。
