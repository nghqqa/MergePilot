# CONTROLLED_REFERENCE_RAG_PILOT — 收口报告

日期：2026-09-26 · 基线链：81e8bdd（授权执行）→ 0015c61（监控建立）→ 本次收口
授权有效期原至 2026-10-02，经 owner 指示缩短为 6 小时后收口。

## 监控期摘要

| 轮次 | 时间 | 结果 | 事件 |
|---|---|---|---|
| 1-3（手动） | 2026-09-25 ~23:30 | 12/12 OK ×3 | 零异常 |
| 4（定时） | 2026-09-26 ~05:37 | 12/12 OK | 零异常 |
| 5（最终+收口） | 2026-09-26 ~06:00 | 12/12 OK | 零异常 |

总计 5 轮 × 12 项 = 60 项检查，**全绿、零 degraded、零停止条件**。

## 收口流程（closeout.mjs 5/5 PASS）

| # | 步骤 | 结果 |
|---|---|---|
| C1 | A 链关闭：`a_chain_disabled` | ✓ |
| C2 | 两 PR 零变化：receipts 4 / tickets 1 / audit 2（前后一致） | ✓ |
| C3 | PG audit 可读（count=2） | ✓ |
| C4 | MinIO 证据可读（staging-ops/evidence.json） | ✓ |
| C5 | 收口后 health=200 | ✓ |

## 授权范围摘要（保留）

- 操作员：仅 pilot（未新增）
- 仓库：wookat/speaktype#426 + nghqqa/tizhou#2（未扩大）
- A 链：reference-only / lexical-zh-en-v1 / 不参与风险决策
- GitHub：零写入（全程）
- C 链/embedding/pgvector/model cache/Fixer/Verifier/RUN_BINDING_AUTH：全程关闭

## 保留工件

- `verification/gate/RPD-LEDGER.json`（status=PILOT_CLOSED）
- `verification/gate/AUTHORIZATION-REQUEST.md`
- `verification/rag-org-knowledge/authorization.log`
- `verification/rag-org-knowledge/pilot-events.jsonl`（5 轮事件流）
- `verification/rag-org-knowledge/pilot-final-report.json`
- `verification/rag-org-knowledge/A-CHAIN-OPS-MANUAL.md`

## 最终状态

- staging 127.0.0.1:48200：运行中、health 200、POSTGRESQL_LIVE、A 链**已关闭**
- 两 PR 数据完好（4/1/2）
- RPD-LEDGER：`PILOT_CLOSED`
- 定时任务：已由一次性收口任务替代（任务本身已自然完成）

## 判定

**CONTROLLED_REFERENCE_RAG_PILOT_MONITORING_STABLE → PILOT_CLOSED**

不宣称：生产上线 · 完整 RAG 已完成 · C 链已就绪 · 自动修复启用 · GitHub 写入开启。
