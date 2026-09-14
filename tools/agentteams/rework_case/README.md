# rework_case — 多 Agent 返工闭环的机制验证案例（决赛 D1）

配套 `tools/agentteams/rework_loop_harness.py`。

## 证据等级：MECHANISM VERIFICATION（不是真实 Agent 运行）

| 真实 | 受控输入 | 未执行（另列证据等级） |
|---|---|---|
| `controller.py::process_event`（旧路径，含 MAX_VERIFY_ATTEMPTS 返工循环） | Reviewer findings（`reviewer_findings.json`） | Matrix / Element 交接 |
| PostgreSQL 审计链：task_runs / stage_runs / stage_events / dispatch_outbox | Fixer 的两个补丁（`attempt1/`、`attempt2/`） | CoPaw 容器、LLM 调用 |
| 验收测试真实执行（`base/test_payments.py`）决定 VERDICT | — | GitHub 写入 |

## 案例：支付幂等 bug

- `base/payments.py`：`charge()` 无条件追加 → 重试请求重复扣款（Reviewer 风险依据 F1，L1）。
- `attempt1/payments.py`：**看似合理但错误**——按 `payment_id` 去重；网关重试带新 `payment_id`，验收测试
  `test_second_payment_request_for_same_order_is_idempotent` 仍失败 → Verifier 判 FAIL → 控制器退回 Fixer。
- `attempt2/payments.py`：按 `order_id` 去重 → 5/5 通过 → PASS。

## 场景

A 退回重修再通过 · B 连续 3 次 FAIL → HOLD(`verify_max_hold`) · C 缺验收测试 → BLOCKED → 退回/上限转人工 · D 非法输入（非 verifier 的 verify、重复 event_id、缺 VERDICT 快照、非 admin 提交）

## 运行

```bash
python tools/agentteams/rework_loop_harness.py --pg-port 55432 --pg-password-file <file> --out evidence/FINALS-REWORK-LOOP-20260914
```
