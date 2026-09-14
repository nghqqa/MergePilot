# 迁移方案包 · orders-schema-change / candidate-a rev2

本包由 `tools/dbverify/run_migration_loop.py` 在**真实 SQL 验证**通过后生成，所有版本信息与验证报告摘要绑定；
任何一项与目标环境不符，即视为本方案**不适用**，必须重新验证。

> 证据等级：ISOLATED_POSTGRES（从脱敏基线 TEMPLATE 克隆的独立 PostgreSQL 试验库）。
> **不是** Agentic Database 分支验证；PolarDB 保持 NOT CONNECTED。试验库通过 ≠ 生产发布。

## 适用版本（必须逐项核对）

| 项 | 值 |
|---|---|
| 候选代码版本（git tree SHA，非远端提交） | `a7db33c36313f5f4c963bd6000b108ca8279d96a` |
| 迁移脚本摘要 sha256 | `5c3d64e04bad548136b63b50e87d26b83af5ada425c7aee052bcb9958623069e` |
| 数据基线 schema 摘要 | `594cb9c48d2db429165fb9a6c1832d8b0bbde42740b67c2d47978fa437ca0c4f` |
| 数据基线 data 摘要 | `e3e3c59da7deaa85b1c8dc8b9dd1371bd931fc81b0aa8e2ccf2aeb1ca3135bf9` |
| 基线行数 | {"customers": 500, "orders": 10000, "payments": 10002, "legacy_order_owner": 120} |
| 验证实例 | test:isolated-postgres |
| PostgreSQL | 16.14 (Debian 16.14-1.pgdg12+1) |
| 验证记录 | `mv-9dc16f1fb9ef07f31aa6808e`（report_digest `38137b62f591a38456a1e314afb205bb84712dd0f658d461659b43e7e84b0d7e`） |
| 断言 | 11/11 通过 |

## 执行顺序

1. `01-preflight.sql`（只读）：目标库的历史数据画像必须与验证基线一致（137 NULL / 2 重复 / 120 可回填）。任一不符 → **停止**，回到验证环节。
2. 执行前调用 `db_release_gate(<ticket>, <目标数据摘要>)`，必须返回 `valid=true`；否则 **停止**（旧批准已失效）。
3. `02-migrate.sql`：单事务；失败即自动回滚，目标库无残留（回填审计表与去重归档表在同一事务内创建）。
4. `03-postcheck.sql`：与试验验证相同的断言；任一不符 → 执行 `04-rollback.sql` 并停止后续发布步骤。
5. 应用发布：先发布新代码（总是携带 customer_id），旧 worker 依赖 `DEFAULT 0` 在窗口期内继续可写；窗口期结束后处理 `orders_backfill_audit.source='sentinel'` 的 17 条记录，再评估移除 DEFAULT。

## 失败停止 / 恢复

- 迁移事务失败：PostgreSQL 自动回滚，无需人工恢复；记录 SQLSTATE 并回到验证环节。
- 后检失败：执行 `04-rollback.sql`（恢复归档支付、恢复回填前的 NULL、移除约束），保留审计/归档表直到确认。
- 任何步骤失败后**不得**继续应用发布；票据保持在 APPROVED 但闸门会因数据摘要变化而返回失效。

## 文件

- `01-preflight.sql` · `02-migrate.sql` · `03-postcheck.sql` · `04-rollback.sql` · `manifest.json` · `SHA256SUMS`
