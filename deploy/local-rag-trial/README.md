# LOCAL_RAG_TRIAL — RAG 本地试验闭环（feat/local-rag-trial）

基线 `1a212fb`（生产候选 head，PR #237 保持 OPEN 只读）。目标：**可试用的
RAG 闭环**——ingest→清洗→分块→去重→嵌入→pgvector 索引→六状态查询 API→
带引用前端→Review 辅助证据（安全边界锁死）。不宣称生产级完整 RAG。

| 维度 | 实现 |
| --- | --- |
| 嵌入 | `local-hash-v1` 确定性哈希嵌入（256 维，L2 归一；sha256 token 投影；中=单字+二元组/拉丁=小写词）+ 远程 provider 占位（不可达→显式 provider_unavailable） |
| 模型绑定 | `model_digest` = 规格说明书 canonical-sha256；规格任何变化→digest 变→索引失效 |
| 分块/引用 | 空行分段+合并+超长二次切分；每 chunk 带 line_start/line_end/char 偏移/para_index/chunk_sha256 |
| 索引 | pgvector `vector(256)` cosine；行级绑定 repo/branch/doc_path/doc_sha256/model_digest/index_version；保留最近 2 个版本供回滚 |
| 原文归档 | MinIO 内容寻址（sha256 键）+写后读回校验（复用 fxv 零依赖 SigV4 客户端） |
| 查询状态 | hit / empty / model_missing / index_stale / provider_unavailable / error（+score floor 防低分冒充） |
| 无引用防线 | 缺引用行检索期丢弃+计数+审计，绝不返回 |
| Review 边界 | RAG=reference only；不自动 finding/ticket/gate；Fixer 剔除 RAG-only 输入；Verifier 只认 harness/test（契约测试锁死） |
| 观测 | ragtrial.query_log（P50/P95 SQL 内计算）+ audit_events（actor NOT NULL）+ error 态进程内存计数 |

目录：

- `docker-compose.yml` / `.env.example` — 独立栈（pgvector + MinIO + console）
- `corpus/` — 试验语料（8 篇 authored 通用运维/安全/设计文档）+ `qa-set.json`（20 题 Recall@5）
- `scripts/run-e2e.mjs` — 15 项必须测试 + 联调 + manifest + 证据落盘
- `OPERATIONS.md` — 启停/回滚/故障处置/明确不做
- `LOCAL_RAG_TRIAL_REPORT.md` — 终版报告与判定

## 快速开始

```bash
cp .env.example .env && docker compose up -d --build
node scripts/run-e2e.mjs
```

测试（无需栈）：`node --test console/backend/test/ragtrial-*.test.mjs`（单测+契约）
与 `node console/backend/test/ragtrial-pg.integration.mjs`（临时 pgvector 容器）。
