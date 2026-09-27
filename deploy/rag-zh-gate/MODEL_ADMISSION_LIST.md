# RAG 中文/多语生产候选模型准入清单（ZH_PRODUCTION_CANDIDATE_PREPARATION 波）

用途：下一轮授权评估的准入基线。**本清单不构成下载授权**——任何模型入库前必须
获得明确授权（来源+许可+SHA256 manifest 受控分发），本波零下载。

## 准入硬条件（全部满足方可进入评估）

1. 中文或多语语义覆盖（预训练含中文对比学习目标，非英文模型+字节回退）；
2. 许可证明确允许商用（MIT/Apache-2.0/自定义需人工审阅全文）；
3. 固定版本（revision commit）+ manifest v2（文件数/总字节/逐文件 SHA256，
   经 `tools/bge_embed.py --manifest-out` 生成、`--verify-only` 校验）；
4. tokenizer/维度/距离/池化方式固定并在 models 表登记（维度 ∈ {256,768,1024}
   白名单，其它维度需先扩表+回归）；
5. CPU/RAM/磁盘/冷启动在部署预算内（参考预算：常驻 ≤1GB/sidecar、冷启 ≤30s、
   磁盘 ≤3GB；本波实测 bge 1.28GB/170MB/冷启≈7s 量级）；
6. 通过 zh-gate-v1 门禁（阈值在 `qa-set-zh.json` 固化：R@5≥0.90、混合子集≥0.67、
   空准确≥0.75、引用命中=行号回溯=1.0）+ calib/holdout 协议
   （`calibrate-hybrid.mjs`，选择只在 calib 上做，holdout 仅验证）。

## 候选优先级（公开规格，待授权后以实际工件为准复核）

| 优先 | 模型 | 来源 | 许可 | 维度/tokenizer | 预期资源 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **bge-m3** | BAAI/FlagEmbedding（HF） | MIT | 1024/XLM-R SentencePiece（多语 100+，中文强）；dense+sparse+colbert 三模（本栈仅用 dense） | ~2.3GB 权重；CPU 前向慢于 e5，冷启 ~10s 级；常驻 ~300MB | 中文检索基准模型，多语覆盖最广；dense 距离 cosine |
| 2 | **multilingual-e5-large** | intfloat（HF） | MIT | 1024/XLM-R SP，mean 池化+query/passage 前缀（与本栈 e5 用法同构） | ~2.2GB；常驻 ~300MB | 与现有 e5-base-v2 运行时兼容（同 BERT 系前向），迁移成本最低 |
| 3 | **bge-large-zh-v1.5** | BAAI（HF） | MIT | 1024/中文 BERT WordPiece（中文专精） | ~1.3GB；同 bge-en 量级 | 中文单语最强候选；中英混合查询弱于前两者 |

注：以上维度/资源为公开规格预估，**SHA256 以授权入库时的 manifest 为准**（本清单
不预填哈希，避免对未验证工件背书）。许可需在入库时复核 LICENSE 全文。

## 所需授权（缺一不进入评估）

1. 模型来源与版本指定（上表任一或等价多语模型）+ 明确下载/分发许可；
2. 受控入库通道（写入本地 HF 缓存或等价目录，附生成 manifest v2）；
3. 评估授权：在 rag-zh-gate 独立栈复跑门禁（无需其它权限）。

## 现有模型角色（本波已定，不变）

- `local-hash-v1`：确定性回归基线（不入生产语义配置）；
- `bge-large-en-v1.5`：英文生产候选+中文对照（zh R@5=0.31，禁外推）；
- `e5-base-v2`：**仅中文内部试验**（zh R@5=0.8846 但空准确 0.5；前沿面证明
  任何权重/地板配置无法同时满足空准确=1.0 与同义/跨表述≥0.5——能力界）。
