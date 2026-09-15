# RAG 观测→评估→数据集→优化→回测 闭环报告

语料：demo-platform/backend/rag-data/dataset.json (8 documents, 9 chunks, SYNTHETIC/REDACTED demo docs)（**SYNTHETIC/REDACTED**，合成演示文档，非企业语料）。策略版本基线 `v1-unigram-hash256`。

## 1. 观测核验（真实审计记录）

- 审计日志 `demo-platform/backend/rag-data/tool-spans.jsonl`：共 604 行 span，其中 rag.retrieve **127 行**，但只对应 **12 个不同 query_hash**；rag.answer 26 行 / 3 个不同 hash。
- retrieve 结果状态：{"OK":127}；延迟 p50=0ms p95=1ms。
- 结论：span 行数是重复演示的审计记录，**不是独立样本**；日志只存 query_hash，无法反推查询文本，因此评估样本必须单独标注（见 §2）。

## 2. 标注数据集与切分

- 查询集 `demo-platform/backend/experiments/rag-loop/queries.v1.json`（版本 rag-loop-queries.v1）：18 个语义族（意图），54 条查询；**按族切分**：调优 10 族 / 30 条，held-out 8 族 / 24 条。
- 泄漏检查（held-out 每条查询与全部调优查询的最大词元 Jaccard）：max=0.2581，mean=0.1571，超过 0.5 的近似改写对：0 对。
- 指标：文档级 hit@1 / hit@3 / MRR；多 chunk 文档另计 chunk-hit@1（仅 9 条带 chunk 标签）。

## 3. 基线（生产策略 v1）

| 集合 | n | hit@1 | hit@3 | MRR | chunk-hit@1 |
|---|---|---|---|---|---|
| 调优集 | 30 | 70.0% | 90.0% | 0.7928 | 50.0% (n=6) |
| held-out | 24 | 75.0% | 100.0% | 0.8611 | 66.7% (n=3) |

### 3.1 调优集 badcase（基线未命中 top-1 的 9 条）

| query | 期望文档 | 实际 top-1 chunk | 期望文档名次 | 分差 | 与命中块重叠词元 | 与期望块重叠词元 |
|---|---|---|---|---|---|---|
| F01-q2 审查流程里每个 Agent 的职责怎么划分 | doc-mergepilot-review-policy | doc-case-pr3-summary#c0 | 3 | 0.036 | 2 | 8 |
| F04-q1 什么风险级别会触发人工闸门 | doc-human-gate-policy | doc-mergepilot-review-policy#c0 | 6 | 0.056 | 16 | 7 |
| F06-q1 PR #1 用的是哪个仓库 | doc-case-pr1-summary | doc-polardb-branch-plan#c0 | 3 | 0.0019 | 6 | 4 |
| F06-q3 fastapi-boilerplate-demo 这个案例的结果如何 | doc-case-pr1-summary | doc-case-pr2-summary#c0 | 3 | 0.0397 | 6 | 7 |
| F10-q3 会话 N2KQqHVSBsSZc9utWsEeZ5f 对应的是哪条 Trace | doc-agentloop-observability | doc-polardb-branch-plan#c0 | 3 | 0.0017 | 4 | 7 |
| F12-q1 AgentLoop 可见性确认的裁决词是什么 | doc-agentloop-observability | doc-case-pr2-summary#c0 | 5 | 0.1383 | 4 | 3 |
| F12-q2 操作员确认 Trace 可见的判定词叫什么 | doc-agentloop-observability | doc-case-pr2-summary#c0 | 4 | 0.1051 | 5 | 2 |
| F13-q3 RAG 会记录用户问了什么问题吗 | doc-rag-usage-policy | doc-mergepilot-review-policy#c0 | 3 | 0.0119 | 7 | 7 |
| F15-q2 为什么 PolarDB 的状态是 NOT CONNECTED | doc-polardb-branch-plan | doc-case-pr1-summary#c0 | 2 | 0.0133 | 9 | 11 |

## 4. 策略优化（只看调优集）

候选网格 12 个（CJK 二元组 × IDF × 哈希维度），选择规则：MRR → hit@1 → hit@3 → 更小维度。

| 策略 | hit@1 | hit@3 | MRR | chunk-hit@1 |
|---|---|---|---|---|
| cand-unigram-tf-hash256 | 70.0% | 90.0% | 0.7928 | 50.0% |
| cand-unigram-tf-hash1024 | 76.7% | 90.0% | 0.8383 | 83.3% |
| cand-unigram-tf-hash4096 | 73.3% | 93.3% | 0.8344 | 83.3% |
| cand-unigram-idf-hash256 | 70.0% | 90.0% | 0.8048 | 50.0% |
| cand-unigram-idf-hash1024 | 76.7% | 90.0% | 0.8456 | 83.3% |
| cand-unigram-idf-hash4096 | 76.7% | 93.3% | 0.8539 | 83.3% |
| cand-bigram-tf-hash256 | 56.7% | 76.7% | 0.7086 | 66.7% |
| cand-bigram-tf-hash1024 | 80.0% | 80.0% | 0.8428 | 83.3% |
| cand-bigram-tf-hash4096 | 73.3% | 90.0% | 0.84 | 83.3% |
| cand-bigram-idf-hash256 | 50.0% | 70.0% | 0.655 | 50.0% |
| cand-bigram-idf-hash1024 | 66.7% | 83.3% | 0.7714 | 83.3% |
| cand-bigram-idf-hash4096 **←选中** | 76.7% | 93.3% | 0.8594 | 83.3% |

耗时（非确定性，见 timings.json）：基线 held-out 每查询 0.006ms / 建索引 0.35ms；选中策略 held-out 每查询 0.052ms / 建索引 0.57ms（本次运行，2026-09-14）

## 5. held-out 回测（选中策略只评估一次）

| 集合 | 策略 | hit@1 | hit@3 | MRR | chunk-hit@1 |
|---|---|---|---|---|---|
| held-out | 基线 v1-unigram-hash256 | 75.0% | 100.0% | 0.8611 | 66.7% |
| held-out | 选中 cand-bigram-idf-hash4096 | 91.7% | 100.0% | 0.9583 | 66.7% |

- 逐条变化：改善 5 条，**退化 1 条**，不变 18 条。
- 退化案例：
  - F17-q1（F17）文档名次 1 → 2
- 晋级规则（回测前声明）：`heldout.mrr > baseline.heldout.mrr && heldout.hit_at_1 >= baseline.heldout.hit_at_1` → **PROMOTE**。held-out MRR 0.8611 → 0.9583，hit@1 0.75 → 0.9167；满足规则。

## 6. 限制与诚实声明

- 样本规模小：语料 8 篇 / 9 chunk，held-out 24 条查询；单条查询名次变化即影响 4.2% 的 hit@1。结论是“该策略在此合成语料上更优”，不外推到企业语料。
- 标注由团队编写，存在标注者偏差；查询集在测量任何基线前固定，held-out 只评估一次，未针对 held-out 调参。
- 语料 SYNTHETIC/REDACTED 边界不变；本实验优化的是检索策略，不改变数据模式声明。

## 7. 复现

```bash
node demo-platform/backend/experiments/rag-loop/rag_eval_loop.mjs --out <dir>
```
报告 JSON 的 sha256 见同目录 SHA256SUMS；运行环境见 run-meta.json。
