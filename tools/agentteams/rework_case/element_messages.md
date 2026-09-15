# Element 真实交接 · 消息模板（同一 PR，返工案例）

> 用途：获得授权后，操作员在 Matrix 房间发出 kickoff；三个 CoPaw 角色按 SOUL 契约回帖。
> **Element 消息只是交接证据；权威状态在 Controller/PG（第二层）或 AgentTeams projectflow/MinIO（原生层）。**
> 每条消息必须携带：任务 ID、PR、head_sha、测试报告路径、证据路径。占位符 `<…>` 由操作员在运行时填入，不得预填。

## 0. kickoff（操作员 → 房间）

```
TASK_SUBMITTED: {"run_id": "<run-rework-live-YYYYMMDD-HHMM>", "repo": "nghqqa/fastapi-boilerplate-demo", "pr_number": <N>, "branch": "mergepilot/rework-payments-<YYYYMMDD>"}
案例：支付幂等 bug（tools/agentteams/rework_case）。验收测试：test_payments.py（5 项）。
Reviewer：只读审查，给出 finding（文件:行、风险级别、指向的验收测试）。
Fixer：最小补丁，只写 PR 头分支；不得改验收测试。
Verifier：在 PR 头 head_sha 上运行验收测试；只认测试结果，回帖必须含 VERDICT= 独立行。
```

## 1. Reviewer 回帖格式

```
TASK_COMPLETED: <run_id>-review
finding F1 | payments.py:<line> | L1 | acceptance_test=test_payments.py::TestPaymentLedger::test_second_payment_request_for_same_order_is_idempotent
head_sha=<40hex> | evidence=shared/tasks/<run_id>-review/findings.md
```

## 2. Fixer 回帖格式（每次尝试一条）

```
TASK_COMPLETED: <run_id>-fix
attempt=<n> | head_sha_before=<40hex> | head_sha_after=<40hex> | files=payments.py
patch=shared/tasks/<run_id>-fix/attempt-<n>.diff
```

## 3. Verifier 回帖格式（两行契约）

```
TASK_COMPLETED: <run_id>-verify
VERDICT=PASS|FAIL|BLOCKED
tests: python -m unittest discover -s <checkout of head_sha_after> -p 'test_*.py' → ran=<k> failed=<m> | report=shared/tasks/<run_id>-verify/attempt-<n>.log | head_sha=<40hex>
```

## 4. 退回（Controller/PG 层，若接入）

控制器在 verify FAIL 后自动向 Fixer 派发 `verify FAIL(第 <i>/3 次)，回退修复。完成写 TASK_COMPLETED: <run_id>-fix。`；第 3 次 FAIL → `HOLD/verify_max_hold`，转人工。原生层则由 leader 依 projectflow 重派（repair attempts 上限见 AgentTeams 配置）。

## 5. 证据采集清单（运行后）

- Matrix：每条消息的 event_id、sender、ts（通过 homeserver API 导出；截图由操作员在 Element 127.0.0.1:18088 手工截取，不得伪造）
- MinIO：`<run_id>-{review,fix,verify}/result.md`、`meta.json`
- GitHub：分支 commits（head_sha 链）、PR 保持 OPEN、无 merge
- （第二层）PG：task_runs / stage_runs / stage_events / dispatch_outbox 快照
- 成本：AI 网关计量（tokens / requests / 时长）
