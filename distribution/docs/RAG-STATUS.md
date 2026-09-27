# RAG Status

## Current State（按部署路径区分）

**路径 A — 预构建发行包（v0.1.0 镜像，digest `sha256:1056df76…`）**

```
A-chain (org knowledge lexical) = OFF (a_chain_disabled)
C-chain (skill_case_retrieval)  = NOT INCLUDED (Enterprise Roadmap)
embedding                        = NOT INCLUDED
/api/rag-trial/* endpoints       = NOT INCLUDED（发行镜像早于本地 RAG 代码合入）
```

**路径 B — 源码自建栈（`deploy/local-rag-trial/`，本地构建镜像）**

```
local RAG trial (corpus ingest/index/query/delete/rollback) = TRIAL（试用）
default embedding  = local-hash-v1（确定性哈希，256 维，零模型下载）
optional embedding = bge-m3（1024 维，自带工件 + manifest fail-closed）
A-chain (org knowledge lexical) = OFF (a_chain_disabled)
C-chain (skill_case_retrieval)  = BLOCKED（Enterprise Roadmap）
scope gate (RAGTRIAL_ALLOWED_SCOPES) = DEFAULT DENY（未配置 → 查询 403）
```

两条路径统一口径：**不宣称生产级 RAG**；检索结果 reference-only。

## Local RAG Trial（路径 B）

状态：试用（TRIAL）——可用、可自测、边界诚实，不称生产。

- 默认嵌入 `local-hash-v1`：确定性哈希（sha256 token 投影，256 维，L2 归一；
  中文=单字+二元组，拉丁=小写词），零下载、离线可用
- 可选 `bge-m3`：多语语义模型（MIT），已过 zh-gate-v1 质量门并固化为
  `MODEL_CONFIGS v1`（混合打分 0.7 向量 / IDF 词法 / 地板 0.35）；接入需
  自带工件 + embed sidecar + `RAGTRIAL_EMBED_EXPECTED_MANIFEST`（fail-closed）
- 查询六状态：hit / empty / model_missing / index_stale / provider_unavailable /
  error；score floor 防低分冒充；降级永不伪装成功
- 索引版本化（保留最近 2 版）+ MinIO 内容寻址归档 + 写后读回校验
- 操作指南：[LOCAL-RAG-GUIDE](LOCAL-RAG-GUIDE.md)

## A-Chain (Organization Knowledge Lexical Retrieval)

Status: Implemented, verified, CLOSED（默认关，需显式开启）.
- Version: lexical-zh-en-v1 (lexical, no embedding)
- Corpus: org security standards (content-addressed, snapshot_id + SHA256)
- Audit: five fields per retrieval
- Degraded: 503 + explicit marker (never masquerades as success)
- Enable: requires human authorization (MERGEPILOT_ORG_RAG_A_CHAIN=1)

## C-Chain (skill_case_retrieval，案例库)

Status: Enterprise Roadmap（BLOCKED，3 gaps，all require operator action）.

### Gap 1: Approved model cache
- Requires: operator offline model acquisition + SHA256 manifest + signed approval

### Gap 2: Provider metadata attestation
- Requires: case_provider_metadata table + live contract tests + tests_attested=true
- Migration ready (applied on isolated PG)

### Gap 3: RUN_BINDING_AUTH key distribution
- Requires: key generation/distribution/rotation/revocation mechanism
- Contract tests passed (26/26)

在缺口关闭前，任何界面或文档不得声称案例检索可用。

## Prohibited（设计禁止）

- No model downloads（仓库不内置、不代下载任何模型；语义工件必须用户自带）
- No embedding generation without configured provider
- No pgvector outside the source-built local RAG stack
- No RUN_BINDING_AUTH bypass
- RAG 结果不自动生成 finding/ticket/gate/VERIFIED

## Semantic Red Lines

- 检索结果**只返回 reference**（路径+行号+digest+版本），NOT risk decisions
- Retrieval does NOT create findings or alter gate/stage/ticket/success
- Fixer 剔除 RAG-only 输入；Verifier 只认 harness/test
- Degraded NEVER displayed as normal success
