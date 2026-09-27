# LOCAL_RAG_TRIAL_REPORT — RAG 本地试验闭环（feat/local-rag-trial）

判定：**LOCAL_RAG_TRIAL_READY**（可试用闭环；非生产级完整 RAG，边界见 §7/§8）

- 基线：`1a212fb`（生产候选 head）；PR #237 保持 OPEN 未触碰；A 链
  （/api/rag/org-search）与 C 链（/api/cchain/*）行为不变（回归测试锁定）。
- 分支：`feat/local-rag-trial`（worktree `D:\goai\mp-worktrees\local-rag-trial`，本地提交不 push）。
- 栈：compose project `local-rag-trial`（独立网络/卷/端口 48450·15436·9113，
  全部 127.0.0.1）——与 promote*/fxv-stage/mp-stage/coreb 零共享，未挂接任何生产或共享卷。
- 凭据：`.env` 一次性本地值（gitignored；`.env.example` 为占位模板），全程未读取/输出/提交真实凭据。
- 测试：**栈上 e2e 26/26 全绿**（15 项必须场景 + 数据链/联调/指标/manifest/secret-scan）；
  单测 22/22；真 PG(pgvector) 集成 25/25；console 后端全量回归 154/0。
- 证据目录：`evidence/local-rag-trial/2026-09-27T01-33-46-010Z/`（00-summary … 09-secret-scan）。

## 1. 模型与 SHA256 manifest

| 项 | 值 |
| --- | --- |
| 模型 | `local-hash-v1`（确定性哈希嵌入，256 维，L2 归一；拉丁=小写词≥2，CJK=连续段二元组/孤立单字；tf 取 1+ln；sha256 token 投影±sign） |
| model_digest | `51ec16eb2616ed5caf5de58721ad9ab94b912714e9594810140af961c9f4041c`（规格说明书 canonical-sha256；规格任何变化→digest 变→索引失效） |
| index_version | 2（e2e 演练后终态） |
| 检索打分 | 混合分 `0.5·vec(pgvector cosine) + 0.5·lex(token 精确交集/min 双方 token 数)`，地板 0.1 |

代码 SHA256（manifest 全量见证据 08）：embed `781f0f33…`、ingest `f5f76f0b…`、
schema `e2f2b7db…`、store `8f0deb3f…`、api `a2a13445…`、review `f85466d7…`；
语料 9 文件（8 篇 authored 文档 + qa-set.json，逐文件 sha256 在 manifest）。
镜像：pgvector/pgvector:pg16（本地，pull never）、elestio/minio@sha256:25348a25…
（与 fxv 发行包同 digest pin）、local-rag-trial-console:local（本分支源码构建）。

## 2. 数据链与证据

- **ingest**：corpus（8 文档）→ 清洗（CRLF/控制字符/空行折叠）→ 分块（空行段合并，
  超长二次切分；每 chunk 带 line_start/line_end/char 偏移）→ 文档内精确去重（chunk_sha256）
  → 嵌入 → pgvector `vector(256)` 行级绑定（repo/branch/doc_path/doc_sha256/
  chunk_sha256/model_id/model_digest/index_version）→ MinIO 内容寻址原文归档
  （sha256 键 + 写后读回校验，全部 `verified_readback`）。
  证据：`01-ingest-corpus.json`（8×ingested + 归档状态）、`08-manifest.json` counts
  （docs 10 / chunks 16：8 语料 + blank + incremental 演练残留按场景清理）。
- **index**：增量（同路径改内容→仅该文档 updated，旧 chunk 出索引）+ 幂等（重复
  ingest 全 unchanged）+ 版本保留（最近 2 个 index_version，回滚窗口）。
- **retrieval**：`02-retrieval-hit.json` — 命中携带完整引用链
  （repo/branch/doc_path/L起-止/doc_sha256/chunk_sha256/model_digest/index_version），
  摘录与行号可回溯原文；无引用行被丢弃（`dropped_uncited` 计数 + CITATION_DROPPED 审计）。

## 3. API / 前端 / audit / metrics 接线

- **API**（全部会话门禁 401；未接线如实 BACKEND_NOT_WIRED）：
  `POST /api/rag-trial/{query,ingest,delete,eval,review-aux,policy-check,index/invalidate,index/rollback}`、
  `GET /api/rag-trial/{status,metrics}`。查询六状态：hit/empty/model_missing/
  index_stale/provider_unavailable/error（+error_kind pg_unavailable/pgvector_unavailable/internal）。
- **前端**：`/rag-trial` 页（RagTrialPage）——状态色标、引用列（文件·行号·digest·版本）、
  摘录、ingest 面板、指标卡（六状态计数/P50/P95/Recall@5/审计计数）；不渲染无引用内容；
  顶部固定安全边界声明。dist 已构建（`index-VtBPZH2-.js` 含 rag-trial 路由）。
- **audit**：`ragtrial.audit_events`（actor NOT NULL，实测零空值）——INGEST/DOC_DELETE/
  INDEX_INVALIDATE/INDEX_ROLLBACK/EVAL_RUN/CITATION_DROPPED/QUERY_*_MISSING|STALE|
  PROVIDER_UNAVAILABLE 全记录（16 条）。
- **metrics**：`ragtrial.query_log` 真实推导（SQL 内 percentile_cont）；error 态在 PG
  不可写时由进程内存计数补齐并随响应/指标端点透出（S12 验证）。

## 4. 指标（终局证据 07-metrics-final.json）

| 指标 | 值 |
| --- | --- |
| Recall@5（trial-corpus-v1，20 题） | **1.0000**（20/20） |
| 引用命中率 | **85/85 = 100%**（返回命中必带引用；1 行无引用被丢弃计数） |
| 查询延迟 | **P50 = 2 ms / P95 = 6 ms**（37 次真实查询） |
| 状态分布 | hit 30 / empty 5 / index_stale 2 / model_missing 1 / provider_unavailable 1 |

质量边界（如实）：哈希嵌入存在碰撞噪声地板（256 维实测 ≤0.05），词法混合分把
零重叠查询压到 ≤0.03、真命中抬到 ≥0.15；**纯语义近义（零 token 重叠）检索不可用**，
本试验是词法+向量混合的可用检索，不是语义嵌入模型。

## 5. Review 联调结果（安全边界）

- R1：`review-aux` 以 run_id 挂辅助证据 —— 全部 `reference_only`/`trusted:false`/
  排除清单（finding/ticket/gate/fixer_patch_input/verifier_evidence）；
  `attachToRun` 只新增 `review_context.rag_auxiliary`，findings/tickets/gates 原样零改写。
- R2：`policy-check` 用真实 RAG 证据实测 —— 建 finding/ticket/gate **全拒**；
  Fixer 输入剔除 RAG 后为空 → **不可启动**；Verifier 对 RAG 证据**恒拒收**
  （只认 harness_report/test_evidence/independent_run_log）。
- 契约测试 `ragtrial-review.test.mjs` 7/7 锁死以上行为；无任何自动晋升路径。
- 证据：`04-review-aux.json`、`05-policy-check.json`。

## 6. 15 项必须测试（栈上 e2e 26/26，逐项映射）

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | 模型缺失 | PASS S1（model_missing，不伪装） |
| 2 | 模型 digest 漂移 | PASS S2a/S2b（部分漂移→hit+drifted_rows；全量→index_stale；恢复回归）+ 注册路径 409 model_digest_conflict（集成） |
| 3 | provider 不可达 | PASS S3（独立探针容器+死端点→provider_unavailable 显式降级） |
| 4 | 空文档 | PASS S4（0 chunk 入库不崩） |
| 5 | 重复 ingest | PASS S5（幂等 unchanged） |
| 6 | 增量更新 | PASS S6（updated+新内容可检索+旧内容出索引） |
| 7 | 删除后不可检索 | PASS S7（chunks 物理清除+审计行 deleted+检索零命中） |
| 8 | stale index | PASS S8（invalidate→index_stale）+D1（re-ingest→hit@v2） |
| 9 | wrong repo/branch 越权 | PASS S9（跨 scope 零泄漏 scoped empty） |
| 10 | 空结果 | PASS S10（零重叠查询诚实 empty） |
| 11 | 引用缺失 | PASS S11（行被丢弃+计数+审计，无无引用命中） |
| 12 | pgvector 不可用 | PASS S12（停库→503 error/pg_unavailable+内存计数补齐；含进程不崩修复：pg pool error 监听） |
| 13 | 重启恢复 | PASS S13（pg+console 重启，docs/chunks/queries/audits 零丢失） |
| 14 | rollback | PASS S14a/b/c（索引版本回滚 v1 生效；无保留行显式拒绝；栈级 down/up 卷保留索引完好） |
| 15 | secret-scan | PASS S15（仓库门禁脚本 `--path .` 0 命中；`.env` gitignored） |

## 7. 与生产 C 链的差异（不宣称等价）

| 维度 | C 链（生产候选） | 本试验 |
| --- | --- | --- |
| 嵌入/模型 | 真实 provider + model cache 内容寻址 + attestation（三输入 BLOCKED 待授权） | 本地确定性哈希嵌入（无外部模型）；远程 provider 仅占位探针 |
| 凭证/验签 | RUN_BINDING_AUTH HMAC 密钥分发 + rotate | 会话门禁（console 契约 v2）；无机器对机器验签 |
| 索引 | 未部署（BLOCKED） | pgvector 独立栈真部署真检索 |
| 执行边界 | FXV run enforce 门禁 + worker 真栈 | Review/fixer/verifier 边界为**服务端策略函数+契约测试**；真实 worker 栈未接线 |
| 语料 | 组织安全标准（A 链 lexical，reference-only） | authored 通用运维语料（repo/branch 绑定为功能演示） |

## 8. 尚未完成的真实外部依赖

1. **真实嵌入模型**（语义级检索质量）：local-hash-v1 为词法级；外部模型需授权+密钥+预检。
2. **真实 worker 栈接线**：Review/Fixer/Verifier 的边界强制目前在本服务策略层，
   未接到 AgentTeams/FXV 实际执行链。
3. **机器对机器验签**（RUN_BINDING_AUTH 形态）：worker 侧消费 RAG 引用需独立凭证链。
4. **生产 C 链三输入授权**（维持 BLOCKED）：model cache/keystore/provider attestation。
5. GitHub 写入/approve/merge 全程关闭（本波零 GitHub 写操作）。

## 9. 结论

LOCAL_RAG_TRIAL_READY：隔离分支+隔离栈内，数据链/六状态 API/带引用前端/审计指标/
Review 安全边界全部落地并以真实容器栈验证（26/26）；未触碰 PR #237、稳定 staging、
A/C 链；无真实凭据出入；生产化差距如实列于 §7/§8。
