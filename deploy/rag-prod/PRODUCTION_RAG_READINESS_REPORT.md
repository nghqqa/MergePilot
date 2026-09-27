# PRODUCTION_RAG_READINESS_REPORT（feat/rag-prod-readiness）

基线 `main=bc44e63`（canonical promote3 运行 dec398d 未触碰）；分支
`feat/rag-prod-readiness`（worktree `D:\goai\mp-worktrees\rag-prod2`，本地提交不 push）。
隔离评估栈 `rag-prod-eval`（console 48470 / pg 15438 / minio 9115 / bge-sidecar 48471，
全部 127.0.0.1，与 promote3/promote2/local-rag-trial/rag-integration 零共享）。
GitHub 零写操作；C 链 enforcement 保持 off；未创建 latest；未读取/输出/提交真实凭据
（keystore 为脚本生成的合成评估密钥，gitignored）。

**总结论：生产候选能力全部接线并验证（e2e 18/18），但 RAG 状态保持 TRIAL_READY，
不宣称 PRODUCTION_READY**——判定依据见 §1-§6。

## 1. 真实 embedding 是否可用 —— **可用（英文），中文语义为已量化缺口**

- **接入方式（零网络/零凭据/可审计）**：本机 HF 缓存中的 `bge-large-en-v1.5`
  （1.28GB safetensors + tokenizer）+ 自研 `tools/bge_embed.py` 纯 numpy BERT 前向
  （无 torch 依赖，CLS 池化 + L2 归一）。语义自检：paraphrase 0.872 vs 无关 0.368。
- **链式 custody**：模型文件 SHA256 manifest（`bge-large-en-v1.5.manifest.json`，
  manifest 体 sha256=`db1042a0edba…`）→ sidecar `GET /manifest` 原始字节回放 →
  console `RAGTRIAL_EMBED_EXPECTED_MANIFEST` 精确比对（fail-closed）→ models 表
  digest 绑定 → chunk 行 model_digest。启动/查询任何一环缺失或漂移即 BLOCKED。
- **fail-closed 验证**：manifest 漂移 → `model_attestation_failed`（集成测试）；
  provider 输出维度不符 → `dimension_mismatch`（集成+栈）；模型缺失 → `model_missing`
  （回归）；**绝无运行时模型下载**（构建期仅锁 numpy/scipy/tokenizers 版本）。
- **中文缺口（实测）**：bge-large-**en** 对中文反向错排（zh↔en 同义 0.42 <
  zh 无关 0.63）——词表仅 488 个 CJK 单字。生产中文语义检索需要授权分发
  zh/多语模型（bge-large-zh / multilingual-e5 等），本机缓存无此工件。
- **确定性基线保留**：local-hash-v1 并行可用（`S2`），作为测试与回归基线不受影响。

## 2. worker 是否真正持久化 —— **是（PG 表为唯一事实源，全语义验证）**

- `ragtrial.jobs` 表 + `console/backend/lib/ragtrial/queue.mjs` + `tools/rag-worker.mjs`：
  幂等（dedupe_key UNIQUE，重复入队 created=false）、指数退避重试、超时
  （timeout_ms + 心跳）、死信（attempts≥max → dead + 人工 requeue）、崩溃恢复
  （FOR UPDATE SKIP LOCKED + 陈旧心跳重占，CTE 保证 was_stale 基于更新前行计算）。
- 任务绑定 repo/branch/doc_path/content_sha256/model_id；执行走 ingest/delete 幂等路径
  ——重试/重占不产生重复索引（同内容 sha → unchanged）。
- 验证：集成 8 项（入队/认领/完成/重试/死信/复活/重占/指标）+ 栈上 W1-W5
  （API 入队→子进程 worker 执行落索引；确定性崩溃模拟→JOB_RECLAIM 重占完成；
  **停 sidecar→语义任务 provider 故障→queued 重试非死非丢→恢复后重试成功**）。
- 生命周期审计：JOB_ENQUEUE/START/DONE/RETRY/DEAD/REQUEUE/RECLAIM 全落
  ragtrial.audit_events（actor=worker 名，NOT NULL）；队列指标端点
  `/api/rag-trial/queue/metrics`（by_state/死信列表/最老排队时长）。
- MinIO 故障语义：对象归档为 best-effort（失败记录 object_status，不阻断、不丢任务、
  不产生脏索引）——如实设计而非缺陷；provider 故障路径由 W5 覆盖。

## 3. attestation / keystore 是否完成 —— **provider attestation 完成；RUN_BINDING_AUTH 接线完成；生产密钥分发未授权（保持 BLOCKED 语义）**

- **provider attestation**：见 §1 链式 custody；栈上 status 组件
  `semantic_provider=ATTESTED`（manifest_sha256 实测显示）。未配置
  RAGTRIAL_EMBED_EXPECTED_MANIFEST → status 如实 BLOCKED。
- **RUN_BINDING_AUTH（机器间）**：`POST /api/rag-trial/machine/query` 复用 cchain
  `verifyRunBindingAndAudit`（HMAC-sha256 + nonce 防重放有界化 + ±5min 时间窗 +
  拒绝审计封顶 + actor=run-binding:<run_id>）。栈上矩阵 M1-M5：有效签名 200 +
  辅助证据（reference_only + 排除清单含 VERIFIED）；坏签名/同 nonce 重放/时间窗外
  全 401；**无 keystore → RUN_BINDING_AUTH_BLOCKED（不回退匿名）**。
- **keystore 分发/轮换/撤销**：与 cchain 同一 keystore 目录语义
  （`*.key.json`：secret/revoked/expires_at；过期或 revoked 即失效）。评估栈用
  `scripts/bootstrap-keystore.mjs` 生成的合成密钥（gitignored）。**轮换**复用
  `/api/cchain/keystore/rotate`（admin 会话，同 keystore 一处轮换全线生效）。
  **生产密钥分发通道未授权执行**——这是遗留外部输入，不是代码缺口。
- 明确不做：C 链 enforcement 未启用；canonical promote3 未切换。

## 4. 质量指标（evidence/rag-prod/2026-09-27T04-06-44-458Z/benchmark.json）

同一英文语料（8 文档）+ QA 集（20 题 + 6 空查询），走 console 生产代码路径：

| 指标 | local-hash-v1（基线） | bge-large-en-v1.5（真实语义） |
| --- | --- | --- |
| Recall@1 | 0.90 | **0.95** |
| Recall@5 | 1.00 | 1.00 |
| MRR@10 | 0.9417 | **0.9750** |
| NDCG@10 | 0.9565 | **0.9815** |
| 引用命中率 | 1.00 | 1.00 |
| **空结果准确率**（fluent 偏题+乱词） | 0.1667 | **1.0000** |
| 查询延迟 P50/P95 | 7/10 ms | 169/479 ms |

解读：语义模型的核心增益是**空结果准确率 0.17→1.0**（基线的词法地板挡不住
fluent 偏题查询；语义分把"写得通顺但语料没有"的问题正确判空）与排序质量提升；
代价是延迟（~170ms vs ~7ms，含 sidecar HTTP+1.28GB 模型 CPU 前向）。打分地板按
bench 实测校准（语义 0.52：真命中最低 0.582 vs 最差假阳 0.48；基线 0.1 维持）。

## 5. RAG 状态 —— **仍为 TRIAL_READY（生产候选能力齐备，未宣称生产）**

依据：① 中文语义模型缺失（§1）；② 生产密钥分发/真实 provider attestation 服务
（外部 attestation URL）未授权接入；③ canonical promote3 未切换本分支发行物
（按禁令）；④ worker 生产化部署形态（容器化常驻 worker）未定型——当前为
`tools/rag-worker.mjs` + PG 队列（语义全验证）。以上任何一项都不影响现有
TRIAL_READY 能力——它们是"生产 READY"的增量条件。

## 6. C 链仍缺的真实外部输入（维持 BLOCKED，未启用 enforcement）

1. **model cache**：受控分发的模型工件目录（本波语义模型走 sidecar 挂载路径，
   C 链 model_cache 组件语义未变）；
2. **provider attestation 服务**：MERGEPILOT_PROVIDER_ATTEST_URL 指向的真实
   在线 attestation 端点 + 预期 key id（本波 provider attestation 是 sidecar
   manifest 链，不等价于 C 链外部 attestation 组件）；
3. **RUN_BINDING keystore 生产分发**：真实密钥经受控通道写入 keystore 目录
   （本波为合成评估密钥；轮换/撤销机制已验证）；
4. 三项齐备前 `/api/cchain/status` 如实 BLOCKED（禁令：不启用 enforce）。

## 7. 验证清单（全绿）

- 栈上 e2e **18/18**（`evidence/rag-prod/2026-09-27T04-20-15-034Z/`）；
- 生产就绪集成 **17/17**（队列/维度/attestation fail-closed/worker 子进程）；
- RAG PG 集成 **25/25**；console 后端全量回归 **157/0**；secret-scan 0 命中
  （--path 补 keystore-local 排除：运行时合成密钥目录，gitignored）。
- 回归项映射：模型缺失 S1/集成、digest 漂移 S2/集成、provider 不可达 W5、
  维度不匹配 §1/集成、重复 ingest W3/S5、增量更新（上一波 25 项维持）、删除 W 路径、
  回滚 S14（上一波维持）、stale index S8、越权 S9、重启 R1、队列重试 W4/W5、
  pgvector/MinIO 故障 S12（上一波）+MinIO best-effort 语义（§2）、引用完整性
  A2/cite=1.0、secret-scan X2、审计/指标 X1。

## 8. 交付物索引

- 代码：`console/backend/lib/ragtrial/{embed,store,schema,api,queue}.mjs`（扩展）、
  `tools/bge_embed.py`（numpy BERT 运行时）、`tools/rag-worker.mjs`、
  `tools/rag-benchmark.mjs`
- 栈：`deploy/rag-prod/`（compose/bge-sidecar+Dockerfile/manifest/corpus(EN)+QA/
  scripts/{bootstrap-keystore,run-e2e}.mjs/OPERATIONS 待补 README）
- 证据：`evidence/rag-prod/`（benchmark.json + e2e summary/transcript）
