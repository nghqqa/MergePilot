# v3 控制台交接（console/STATUS.md）

**日期**：2026-09-22（M3.5）｜ **状态**：只读 API + 页面已可用（shadow/fixture 数据）

## 是什么

- `tools/console_v3/server.py`：GET-only 本地服务（默认 `127.0.0.1:4190`）。
  - `GET /healthz` → `{"ok":true,"mode":"read-only"}`
  - `GET /api/runs` → run 列表（mode/风险/outcome/coverage_missing/superseded）
  - `GET /api/runs/<run_id>` → 完整读模型（风险/审查器状态/findings 及来源/验证状态/覆盖/降级原因/RAG snapshot/manifest hash）
  - `GET /` → 只读页面（shadow/fixture 徽标；部分完成/手工介入以文字+颜色双通道显示）
- 数据源：`RunStore`（SQLite WAL；路径 env `MERGEPILOT_V3_RUNSTORE`，默认 `~/.mergepilot/v3-runs.db`）。

## 硬边界

1. **无任何写端点**（POST/PUT/DELETE 一律 405）；不实现批准/拒绝/派发/GitHub 写按钮——真实审批需 D-1/D-2 拍板 + R1/R2 授权。
2. 每条记录必须带 `mode` 标签（shadow/fixture）；**shadow/fixture 数据永不显示为真实运行**。
3. 部分完成不得压缩为成功：`coverage.complete=false` 与 `coverage_missing` 逐条可见（测试固化）。

## 生成数据的方式

- shadow：`MERGEPILOT_REVIEW_V3=shadow` 运行桥（真实 PR 只读元数据/diff，无 Agent、无外部写）；
- fixture：测试内 `adapter.run_v3_fixture(...)`（本地假审查器，mode=fixture）。

## 后续（未做，需输入）

- PG 读副本/多实例：随 Controller cutover 迁 PostgreSQLTicketStore 同批评估；
- 真实 run 数据：R1/R2 授权后 on 模式产生的记录将带 `mode=on`；
- 页面美化与运行面板整合：M4 控制台工作项。
