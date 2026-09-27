# RAG_ZH_ADMISSION_REBASE_3131957_REPORT

日期 2026-09-27。分支 `feat/rag-zh-admission`（基线 **main=3131957**=PR#243 合并提交，
本地未 push）。隔离栈 `rag-zh-eval` 六容器（从本分支重建）；promote3=**b2e8727d**
（runtime digest 基线，G-4）全程未动；C 链 off；零 push/PR/merge。

## 结论：**ADMISSION_EVIDENCE_GAPS（唯一剩余缺口 G-2=SOURCE_VERIFICATION_BLOCKED 分量）**

G-1/G-3/G-4/G-5 全部以硬证据关闭；双模型在重建分支上**复现达标**（17/17）。
G-2（hf-mirror revision 与官方仓库交叉核验）因本机官方通道全面不可达而
**SOURCE_VERIFICATION_BLOCKED**——需不受限主机或授权渠道补核后方可转
ADMISSION_EVIDENCE_READY。**RAG 维持 TRIAL_READY。**

## 第一阶段：只读基线确认（全过）

| 项 | 实测 |
| --- | --- |
| live main（gh API） | **3131957** = PR#243 合并提交（**MERGED 12:02:05Z**）✓ |
| eval2=35a4070 | 未入 main（未 push，远端 422）✓ |
| promote3 | b2e8727d（started 09:57Z，healthy，local-hash-v1）未动 ✓ |
| worktree/容器隔离 | rag-canonical-a/rag-f1/rag-integration/rag-prod2 各自分支；三 RAG 评估栈 16 容器与 promote3 零共享 ✓ |

## 第二阶段：重建评估分支

- `feat/rag-zh-admission` off 3131957；**移植 eval2 独有资产**（tools/bge_embed.py、
  tools/rerank_score.py、deploy/rag-zh-eval/** 共 13 文件）——ragtrial 库文件**保留 main
  版本**（F1+机器码 reason+VERIFIED+语言修复，零库冲突：eval2 未改这些文件）；
- F1 行为保留并实测在位：`df_unavailable`/`hybrid_df_fallbacks`/`HYBRID_DF_FALLBACK`
  有界去重 audit/idf-required 503 fail-closed（HYB5-9 + HYB8b/8c/9b 全绿）；
- 模型不入生产面：HYBRID_CONFIGS 默认三档未变（G5b 断言）；bge-m3/zh/reranker 零出现于
  发行物 compose/promote3（grep 实测空）。

## 第三阶段：缺口关闭

| 缺口 | 处置 | 证据 |
| --- | --- | --- |
| **G-1** 转换不可复演 | **关闭**：镜像固定 revision **重下载 bin**（SHA256 与下载时记录逐字节一致：bge-m3 `b5e0ce34…`/bge-large-zh `bf84a56f…`）→ **`tools/bin_to_safetensors.py`（已提交的可复演工具）** 转换 → 输出 safetensors SHA256 与 manifest 锁定值 **双 MATCH**（`bf065381…`/`23bc370e…`） | `evidence/rag-zh-admission/g1-repro-chain.json` |
| **G-2** 镜像↔官方交叉核 | **SOURCE_VERIFICATION_BLOCKED**（本机）：huggingface.co DNS 污染（→31.13.69.169 Facebook 段）；DoH 1.1.1.1/8.8.8.8/dns.google/cloudflare-dns 全部无响应；hf.co 307→污染域。**补救链已备**：G-1 证明镜像按 revision 确定性供数+转换可复演——剩余仅"官方渠道确认 revision/文件"一步，需不受限主机执行 | 同上 g2_official_channel 字段 |
| **G-3** per_pair_ms 单位 | **关闭**：eval-matrix.mjs 修正为 `t1ms/rows`（真实 ms）+ **运行时单位断言**（偏差>1ms 即抛错，防 ×1000 复现） | 脚本内断言 |
| **G-4** 报告基线 | **执行**：本报告起 runtime digest=**b2e8727d**；历史时间线保留（02:4x dec398d→09:3x f7ffce6d→09:55 b2e8727d） | §第一阶段 |
| **G-5** F1×模型覆盖 | **关闭**：G5a（idf-required 覆盖正常路径可用、DF 健康不触发回退）+G5b（默认三档不受覆盖影响） | pg 集成 39/0 |

## 第四阶段：验证（全部实际执行）

- 后端全量 **173/0**（含 #243 后新增测试）；ragtrial 套件：单测 26 / **pg 集成 39**
  （HYB1-9+G5）/ prod 17 / canonical 18（MF1-6 fail-closed 矩阵）；secret-scan **0**；
  前端构建 ✓；
- **双模型全集门禁（重建分支+重建栈，console 全链）17/17 复现达标**：
  bge-m3 R@1 0.7308/R@5 **0.9615**/同义跨表述 1.0/混合 0.8333/空准确 **0.875**/
  引用=回溯=1.0/P50 145ms P95 208ms；bge-large-zh R@1 0.6923/R@5 **0.9615**/空准确
  0.875/P50 168ms P95 211ms——**与 eval2 分支数字一致**（P50 略优=重建后缓存更热）；
- calibration 复跑：三基线模型判定可复现（同配置同结论；新模型走 console 注入校准）；
  calib/holdout/全集严格分离维持（阈值预声明不变）；
- manifest/维度/缺文件/额外文件/digest 漂移 fail-closed：三 manifest 现验 OK + MF 测试；
- 引用回溯/scope 403/验签矩阵（M1-M6）/audit/worker/重启恢复：canonical 集成+门禁实测；
  **备份恢复**：admission 栈 pg_dump→临时容器 12/3 全量还原 OK。

## 准入判定

- **模型质量与供应链自证链：READY**（G-1 硬证据+门禁复现）；
- **来源官方核验：BLOCKED（G-2）**——转 ADMISSION_EVIDENCE_READY 需任一：
  ①不受限主机对 huggingface.co 官方 API 核 revision+文件哈希；②授权的官方渠道重新
  受控入库（MODEL_ADMISSION_LIST 既定路线）；③用户对镜像来源的明确追认；
- 生产四前置不变（模型正式授权/keystore/attestation/promote3 试点）；
  **RAG 维持 TRIAL_READY**；不因此接入 promote3 或启用 C 链。
