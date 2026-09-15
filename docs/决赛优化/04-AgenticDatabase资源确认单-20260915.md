# PolarDB PostgreSQL · Agentic Database 资源确认单（2026-09-15）

> 状态：**NOT_EXECUTED**。本单**单独推进**，与本地 SQL 验证（`LOCAL_REAL_SQL`，独立 PostgreSQL 克隆库）**不混称**：
> 本地验证证明的是"迁移在历史数据上会失败 / 修订后通过 / 批准绑定版本 / claim 过闸"这套**机制**；
> Agentic Database 分支验证证明的是同一机制在**官方产品分支**上成立。两者的证据目录、等级标签分开。

## 一、需要官方/主办方确认的资源与事实

| # | 确认项 | 为什么需要 | 我们这边已就位 |
|---|---|---|---|
| D1 | Agentic Database Branch 是否已对 **PolarDB PostgreSQL** 开放（公测/白名单）；可用地域 | 决定"降级树"停在第 1 层还是第 2 层（影子库/克隆） | 降级树与声明口径（复赛技术推进书 WP4） |
| D2 | 分支 API：创建 / 列表 / 销毁 / 过期与自动清理；每分支容量与并发上限 | `trial_kind='AGENTIC_DB_BRANCH'` 的登记字段需要分支身份（`trial_instance`）与销毁凭证 | m9 `migration_verifications.trial_kind` 已含 `AGENTIC_DB_BRANCH` 枚举 |
| D3 | 从**脱敏基线**创建分支的方式（快照 / 逻辑导入）与耗时 | 基线摘要（`data_baselines.data_digest`）必须在分支上可复算 | `tools/dbverify/data_digest.py`（规范算法）、基线登记函数 |
| D4 | 只读账号 + 迁移执行账号的最小权限模板；网络白名单（本机公网出口） | 8 项接入门槛中的 endpoint / 只读账号 / 白名单 / Branch API 凭据 | `polardb_adapter.evaluateLiveGates()` 8 门槛检查已在代码中 |
| D5 | 费用：分支按量计费口径、免费额度、预算告警 | 决赛演示预算与"失败时保护措施"叙事 | 方案包 README 的失败停止/恢复步骤 |
| D6 | 版本/扩展差异：PolarDB PG 主版本、`vector` 扩展可用性、`pgcrypto`(`digest()`)、`gen_random_uuid()` | 审计域迁移链依赖 pgcrypto/pgvector；需核对与生产 PolarDB 的差异 | 迁移链 000→005 在 PG 16.14 + pgvector 0.8.5 全量 apply 通过 |
| D7 | 是否允许在分支上跑我们的迁移与断言（写入分支、不落生产） | 这是"真实分支验证"的最小动作 | `run_migration_loop.py --trial-instance <branch-id>` 只需把 TEMPLATE 克隆替换为分支 DSN |

## 二、获得资源后的最小验证步骤（不与本地验证混写）

1. 用官方接口从脱敏基线创建分支，记录分支 id → `trial_instance`，`trial_kind='AGENTIC_DB_BRANCH'`。
2. 在分支上 `data_digest.py --expect <基线摘要>`：一致才继续（否则基线口径不成立）。
3. 在分支上执行 rev1（预期 23502 失败）、rev2（预期 PASS 11/11），回写 `mv_record_verification`。
4. 票据绑定 → 批准 → `l2_claim_ticket(..., <分支现算摘要>)` 过闸；销毁分支；记录费用与耗时。
5. 证据目录 `evidence/FINALS-AGENTIC-DB-<日期>/`，等级标签 **REAL_EXECUTED（Agentic Database Branch）**；README/响应文档把 PolarDB 边界从 NOT CONNECTED 改为"分支验证已执行，生产未接入"。

## 三、若确认无法获得

保持 `PolarDB = NOT CONNECTED`、`Database Branch = SIMULATED（三候选对照）+ LOCAL_REAL_SQL（候选 A 真实链）`，答辩按降级树逐级声明，不宣称已完成官方分支验证。
