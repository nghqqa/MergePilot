# RAG_ZH_MODEL_RERANKER_EVALUATION_REPORT

日期 2026-09-27。分支 `feat/rag-zh-eval2`（基线 zh-gate@78eb98d，未 push）。隔离栈
`rag-zh-eval`（独立 PG 15440/MinIO 9117/console-m3 48485/console-zh 48486/双 sidecar
48487-48488；模型缓存 `D:\goai\rag-zh-models` 仓库外隔离）。promote3=dec398d 未动；
C 链 off；canonical 未切换；**未接入任何生产配置**。

**总裁决：出现合格中文生产候选——bge-m3 与 bge-large-zh-v1.5 双双通过预声明全集门槛
（console 全链 17/17）。RAG 状态仍为 TRIAL_READY**（生产化前置=正式模型授权/
keystore/attestation/promote3 试点，均未执行；本波为隔离评估）。

## 1. 来源、许可证、版本与 manifest（先核实后评估）

| 模型 | 来源 | 许可 | 固定 revision | 交付形态 | manifest v2 |
| --- | --- | --- | --- | --- | --- |
| bge-m3 | hf-mirror.com/BAAI/bge-m3（huggingface.co 直连不可达——DNS 污染，如实披露；镜像与官方同源） | MIT（README+API tag 双核） | 5617a9f61b028005a4858fdac845db406aefb181 | 2293MB/10 文件（bin→safetensors 宿主一次性转换后删除 bin，单权重形态） | pin `7ae16e546ae24c49…`，三重校验 OK |
| bge-large-zh-v1.5 | hf-mirror.com/BAAI/bge-large-zh-v1.5 | MIT | 79e7739b6ab944e86d6171e44d24c997fc1e0116 | 1303MB/10 文件（同上转换） | pin `7d5fc818f4455e70…`，三重校验 OK |
| bge-reranker-v2-m3 | hf-mirror.com/BAAI/bge-reranker-v2-m3 | Apache-2.0 | 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e | 2293MB safetensors 原生 | pin `95363d30…`，OK |

供应链：下载脚本+全套逐文件 SHA256（`D:\goai\rag-zh-models\sha256-all.txt`）；
`tools/bge_embed.py` 新增 **torch .bin 受限读取器**（白名单 unpickler+storage 桩，
零 torch 依赖、零任意代码执行）与 **XLM-R 适配**（SP 分词/位置偏移/eps）；
manifest.json 自引用已从扫描/盘点中排除（自指哈希无意义）。
对照保留：local-hash-v1 / bge-large-en-v1.5 / e5-base-v2（**e5 继续仅内部试验**）。

## 2. 实验矩阵（calib 选择/holdout 只验证；反降阈规则不变）

矩阵 5 模型 × {emb-only, hybrid, hybrid+rerank}（证据 `evidence/rag-zh-eval/
2026-09-27T10-29-01-187Z/matrix.json`，chunk 行复用 zh-gate 索引保证引用一致）：

| 模型 | emb-only calib/holdout R@5 | hybrid calib（选中配置） | holdout | +reranker calib/holdout |
| --- | --- | --- | --- | --- |
| local-hash-v1 | 0.08 / 0.00 | 0.5385（w.3 plain f.15） | 0.7692/空.75 | —（基线不评） |
| bge-large-en | 0.15 / 0.00 | 0.6154 | 0.7692/空.75 | — |
| e5-base-v2 | 0.15 / 0.00 | 0.6154 | 0.7692/空.75 | **0.9231/0.8462**（唯一被 rerank 救活） |
| **bge-m3** | 0.8462 / 0.6923 | **1.0000**（w.7 idf f.35，四类全 1.0） | **0.9231**/空.75 | 0.9231/0.8462（**无增益**） |
| **bge-large-zh** | 0.7692 / 0.4615 | **1.0000**（w.7 plain f.30） | **0.9231**/空.75 | 0.9231/0.8462（无增益） |

**reranker 结论**：交叉编码器判别力极强（相关 +3.86 vs 无关 -6.61 logit），能把 e5
抬过能力界（首个 eligible），但对已达标的双新模型**无质量增益**（calib 0.92<1.0），
且 CPU 代价 1440-2634ms/对（top-30 重排=43-79s/查询）——**在线不可行，仅离线批量场景**。
门槛达标不需要 reranker；其价值在于低质嵌入兜底与离线深度评估。

## 3. 全集正式门禁（console 全链，预声明阈值，不因结果调整）

校准配置经 `RAGTRIAL_HYBRID_JSON` 注入（该机制设计用途；生产化时固化进
HYBRID_CONFIGS）。**默认 0.52 地板不适用新模型**（bge-en 校准值，实测压制新模型
召回至 0.27——floor 必须按模型校准，已如实记录）。全集 26 QA+8 空（zh-gate-v1）：

| 指标 | bge-m3 | bge-large-zh-v1.5 | 门槛 |
| --- | --- | --- | --- |
| **Recall@1 / @5** | **0.7308 / 0.9615** | **0.6923 / 0.9615** | @5 ≥ 0.90 ✅ |
| 同义 / 跨表述 / 混合 recall@5 | **1.0 / 1.0 / 0.8333** | **1.0 / 1.0 / 0.8333** | ≥0.5/≥0.5/≥0.67 ✅ |
| MRR@10 | 0.8533 | 0.81 | — |
| **空结果准确率** | **0.875** | **0.875** | ≥0.75 ✅ |
| **引用命中 / 行号回溯** | **1.0 / 1.0** | **1.0 / 1.0** | =1.0 ✅ |
| P50 / P95 延迟 | 169 / 361 ms | 174 / 280 ms | 预算内 ✅ |
| **判定** | **达标（七项全过）** | **达标（七项全过）** | 17/17 GREEN |

门槛其余项：manifest/digest/维度 fail-closed（注册链+sidecar 启动验证+集成测试）；
scope 403（双模型实测）；RAG reference-only 边界（VERIFIED/fixer/verifier 全拒实测）；
**重启恢复**（pg+console 重启 12/12/3 零丢失）与 **备份恢复**（913 行 dump→临时容器
12/12 还原）实测 OK；secret-scan 0。

## 4. 资源与延迟成本（实测）

- 常驻内存：m3-sidecar **1.27GB** / zh-sidecar **1.01GB**（memmap safetensors）；
  console 33MB / pg 62MB；
- 冷启动（restart→healthy）：bge-m3 **12.9s**；
- 索引尺寸：12 文档×1024d ≈ 432KB/模型（chunks_semantic）；
- worker 吞吐（local 任务基线）：445ms/10 任务；语义摄取 12 文档<30s；
- 查询 P50/P95：见 §3（169-174ms / 280-361ms，CPU-only 前向）；
- reranker（若部署）：+1-2.6GB 常驻 + 不可行在线延迟（§2）。

## 5. 模型结论与推荐

- **推荐生产主选：bge-m3**（多语覆盖最广、混合子集更强 1.0、与 reranker 同生态）；
  **备选：bge-large-zh-v1.5**（中文专精、内存低 20%，全集同分）。两者全集指标相同，
  按部署偏好取舍。
- bge-large-en：仅英文（zh 全集 R@5 0.31 禁外推）；e5-base-v2：**继续仅内部试验**
  （本波 rerank 组仅证明"可被救活"，不改变其定位）；local-hash-v1：确定性回归基线。
- **合格中文生产候选：已出现**（§3）——但"候选达标"≠"生产就绪"（见下）。

## 6. RAG 状态与生产化前置

**RAG 仍为 TRIAL_READY。** 达标模型尚未接入任何生产/canonical 配置；转生产需：
1. **正式模型授权**：bge-m3（或 bge-large-zh）经准入清单流程确认（来源=本波镜像
   下载需追认或改由官方渠道重新受控入库；许可 MIT 已核；manifest 已固化）；
2. **keystore 生产分发**；3. **external attestation 接入**；4. **promote3 试点**
   （发行物化+双回滚锚点+受控流量）。四项齐备前一切维持现状。

## 7. 禁令遵守与回归

未修改 promote3/canonical；未启用 C 链；未 push/建 PR/merge；未创建 latest；
未读取/输出/提交真实凭据（评估栈一次性值+无 keystore 分发）；**未接入生产配置**
（隔离栈+RAGTRIAL_HYBRID_JSON 评估注入）。回归：单测 25+pg 29+prod 17+canonical 18
+后端全量 **157/0**+secret-scan 0。全程只下载了任务点名的三个模型（hf-mirror 来源
如实披露，huggingface.co 直连不可达）。
