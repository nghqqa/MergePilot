# RAG_ZH_MODEL_QUALITY_GATE_REPORT

日期 2026-09-27。分支 `feat/rag-zh-gate`（基线 `feat/rag-canonical-a@7c3775b`，worktree
`D:\goai\mp-worktrees\rag-canonical-a`，本地未 push）。**结论：本地无合格中文/多语生产
候选模型——两语义候选按预声明门槛均判未达标；RAG 维持 TRIAL_READY。**
e5-base-v2 为实测出的最强过渡选项（低于门槛，见 §3），已建立完整可复用的中文评估基建。

## 1. 候选模型盘点（本地缓存全量 triage：36 个模型）

**合格中文/多语文本嵌入模型（有权重）：零。** 逐一核查（有真实权重 vs 仅 ref）：
- `urchade/gliner_multi-v2.1`（唯一多语编码器候选=mDeBERTa 背骨）：**仅 ref 无权重**；
- `facebook/w2v-bert-2.0`：语音模型（非文本嵌入）；Paddle 系 15 个：视觉/OCR/表格；
  `answerdotai/ModernBERT-base`：仅 ref。
- **可运行文本嵌入候选**（许可证自本地 README 核实）：

| 模型 | 许可 | 版本/快照 | 维度 | tokenizer | 距离 | 文件/字节/SHA256 |
| --- | --- | --- | --- | --- | --- | --- |
| bge-large-en-v1.5 | MIT（FlagEmbedding，商用免费） | d4aa6901… | 1024 | WordPiece 30522（**488 CJK 单字**） | cosine, CLS+L2 | manifest v2：10 文件/1342MB/逐文件 sha256+bytes；manifest 体 pin `05c8959d…` |
| e5-base-v2 | MIT | f52bf8ec… | 768 | WordPiece 30522（488 CJK） | cosine, **mean** 池化+query/passage 前缀 | manifest v2：9 文件/439MB；pin `072bb4e5…` |
| local-hash-v1 | 仓库自有 | — | 256 | 词+CJK 二元组 | cosine | 确定性基线（角色=测试/回归，不作生产语义候选） |
| gte-modernbert-base | （本地未核） | 288MB 权重在 | 768 | byte-BPE | cosine | **未纳入评估**：需 rotary 注意力运行时（本波未实现）；且英文训练+字节回退分词，zh 上限弱于 e5，优先级最低 |

CPU/RAM/磁盘（实测，宿主 15.6GiB）：bge sidecar 权重 1.28GB 磁盘/常驻 ~170MB（memmap，
推理时峰值升高）；e5 439MB/常驻 ~47MB；纯 CPU 前向无 GPU 依赖。下载分发：**本波零下载**
（无授权来源；HF 缓存只读挂载）。

## 2. 中文评估基建（可复用，授权模型到位即可复评）

- **隔离栈 `rag-zh-gate`**（与 promote3/rag-prod-eval 等零共享；console 48480 + console-e5
  48483 双实例共享本栈 PG、bge-sidecar 48481、e5-sidecar 48482、pg 15439、minio 9116，
  全 127.0.0.1，独立网络/卷）。
- **评估集 zh-gate-v1**：12 篇多段中文语料（多主题运维/安全/审计/密钥/监控）+ 26 分类 QA
  （直接 4/同义 8/跨表述 8/中英混合 6）+ 8 空查询（fluent 偏题 6 + 乱码 2）；
  **阈值执行前预声明**（R@5≥0.90、混合子集≥0.67、空准确≥0.75、引用命中=1.0、回溯=1.0）。
- 每个命中做**引用回溯**（snippet 必须来自原文 line_start..line_end，空行归一后比对）；
  越权仓库/分支 403（scope allowlist 继续生效）；RAG 结果 policy-check 全拒
  （finding/ticket/gate/VERIFIED）；worker/审计/metrics 全链在栈上运行。

## 3. 中文质量指标（真实执行，与英文对照；不用英文替代中文）

评估集 zh-gate-v1（k=10 候选池；证据 `evidence/rag-zh-gate/2026-09-27T07-26-17-295Z/`）：

| 指标 | local-hash-v1（基线） | bge-large-en-v1.5 | e5-base-v2 |
| --- | --- | --- | --- |
| Recall@1 / @5 | 0.50 / 0.6154 | 0.3077 / 0.3077 | **0.6538 / 0.8846** |
| MRR@10 / NDCG@10 | 0.8958 / 0.5678 | 1.0000 / 0.3077 | 0.8069 / 0.7887 |
| 同义查询 recall@5 | 0.50 | **0.00** | **0.875** |
| 跨表述 recall@5 | 0.375 | **0.00** | 0.75 |
| 中英混合 recall@5 | 0.8333 | 0.6667 | **1.000** |
| 空结果准确率 | 0.75 | **1.00** | **0.50** |
| 引用命中 / 行号回溯 | 1.00 / 0.844 | 1.00 / 1.00 | 1.00 / 1.00 |
| P50 延迟 | 8 ms | 154 ms | 60 ms |

英文参照（canonical 波英文集）：bge-en R@1=0.95/空准确=1.0 —— **同模型中文 R@5=0.31、
同义/跨表述=0/8**，外推陷阱被完全量化。

**门槛判定（预声明阈值，执行后不可调）**：
- bge-large-en-v1.5：**未达标**（R@5 0.31≪0.90；混合 0.67 达标但同义/跨表述全灭）；
- e5-base-v2：**未达标**（R@5 0.8846<0.90 差 2 题；**空准确 0.5<0.75——fluent 中文偏题
  查询产生假阳性**，语义分离度不足是硬伤）；
- local-hash-v1：确定性基线（不参与语义候选判定；其 0.62/0.75 是词法上限的诚实参照）。

## 4. 资源与延迟成本（实测）

- 常驻内存：bge-sidecar 170MB / e5-sidecar 47MB / console 33MB / pg 55MB（CPU 峰值在
  前向期间，稳态 <1%）；
- 索引大小（pgvector）：chunks(256d) 224KB / chunks_semantic(1024d) 360KB /
  chunks_semantic_768 240KB（12 文档三模型）；
- 冷启动：e5 sidecar restart→healthy **6.8s**；worker 吞吐：10 个 local 任务 **445ms**
  （≈1300 任务/分钟，语义任务受 sidecar 前向限速）；
- 查询延迟：e5 P50 60ms / bge 154ms / 基线 8ms。

## 5. 推荐与判定

- **保留模型**：英文=bge-large-en-v1.5（英文生产候选不动）；**中文过渡=e5-base-v2**
  （本地最强中文检索实测，但仍低于生产门槛——尤其空准确 0.5，仅可用于内部试验，
  不得宣称生产候选）；zh 词法兜底=local-hash-v1（确定性基线）。**生产中文语义模型：
  本地无合格者，需授权分发**（建议优先评估 bge-m3 / multilingual-e5-large / bge-large-zh-v1.5，
  均需明确来源、许可与 SHA256 manifest 后走本报告 §2 基建复评）。
- **RAG 维持 TRIAL_READY**：中文门槛未过（§3），英文指标不得外推；eval 栈/manifest/
  fail-closed/scope/边界全部保持生效。

## 6. 后续授权清单（进入生产所需）

1. **zh/多语模型授权**：指定模型+来源+许可+manifest 受控入库 → 用本波基建复跑
   zh-gate-v1（阈值已在 qa-set-zh.json 固化）→ 达标后中文生产候选成立；
2. **keystore 生产分发**（真实 RUN_BINDING 密钥受控写入；轮换/撤销机制已验证）；
3. **external attestation 接入**（MERGEPILOT_PROVIDER_ATTEST_URL+预期 key id；
   本地 manifest 验证≠外部 attestation，披露保持 NOT_CONFIGURED 如实）；
4. **promote3 试点**（发行物化整合分支+双回滚锚点+受控流量；单独授权波）。

## 7. 禁令遵守声明

未切换/修改 promote3（dec398d healthy、enforce=0）；未启用 C 链；未 push/建 PR/merge；
未创建 latest；未新增用户/仓库/PR；未读取/输出/提交真实凭据（栈内一次性值+合成密钥）；
未将 RAG 标记 PRODUCTION_READY（**TRIAL_READY 维持**）；未运行时下载任何模型。
