# RAG_ZH_MODEL_ADMISSION_READONLY_RECONCILIATION_REPORT

日期 2026-09-27。**只读调和**——本轮零文件/分支/容器/卷/模型缓存修改、零下载/转换/
分发、零 push/PR/merge、零凭据读取。报告以未跟踪文件存放（分支 35a4070 字节未变）。

## 总结论：**EVIDENCE_VERIFIED（附准入前必补项 5 条，见 §6）**

评估方法、数字口径、供应链证据与运行边界**自洽且可由原始证据复算**；两处口径问题
（promote3 陈旧陈述、rerank_timing 单位错误）均已用硬证据调和，**不构成数据矛盾**。
bge-m3 与 bge-large-zh-v1.5 **具备进入"单独授权的正式准入流程"的证据条件**——
该结论不代表生产 READY；RAG 维持 TRIAL_READY。

## 第一步：状态核实

| 项 | 实测 | 与报告对照 |
| --- | --- | --- |
| live main（gh API） | **6542f97** | 与审查基线一致 ✓ |
| feat/rag-zh-eval2 HEAD | **35a4070** | = 报告 ✓；worktree tracked-clean ✓；**未 push**（远端无此分支）✓ |
| F1 分支 | **fix/rag-f1-idf-failclosed @ e37dac6**（并行会话：把 canonical-a→zh-prep 链 rebase 到 main=6542f97 为单提交链 59bfdf8→e553460→f9edb2e→4e25d65，+两 F1 提交 887a6e1/e37dac6） | 报告称 e37dac6 ✓；**已 push，PR #243 OPEN（未合并）**；与 eval2 **零共同提交**（merge-base=bc44e63 内容层）——**两谱系独立，未混接** ✓；树差异 4e25d65 vs 78eb98d = 语言波(#241/#242)内容差异，ragtrial 代码等价 |
| promote3 运行镜像 | **b2e8727d**（tag `rag-trial-20260927-langfix`，创建 09:55:30Z，容器 09:57:09Z，healthy）；compose pin=b2e8727d；model=**local-hash-v1**（POST_LANGUAGE 文档明示未接 e5/bge） | 见下方口径调和 |

**promote3 口径调和（dec398d vs b2e8727d）**：三份并行会话文档钉死时间线——
02:4xZ dec398d（RAG Trial 首接，main=1d7ab98）→ 09:3xZ f7ffce6d（mainline 切换演练）→
**09:55Z b2e8727d（langfix 重建，main=6542f97，当前运行）**。本会话波次在 09:57Z 之后
撰写的报告仍写"promote3=dec398d 未动"——**陈旧陈述**：含义上"本波未触碰"为真，
但栈的绝对状态已被并行会话合法更新。判定：**时间差 + 陈旧口径，非数据矛盾、非栈名差异**。
以当前实测为准：b2e8727d / healthy / C 链 BLOCKED（compose `MERGEPILOT_CCHAIN_ENFORCE: "0"`
+ keystore 目录存在但两份并行验证文档均记录 MISSING/not_distributed）。直接 API 复测
因 .env 变量名未匹配未成功（如实记录；以上述 compose env+文档双源为证）。

## 第二步：模型供应链证据

| 检查 | bge-m3 | bge-large-zh-v1.5 | bge-reranker-v2-m3 |
| --- | --- | --- | --- |
| 上游仓库/许可 | BAAI/bge-m3，MIT（README license 行+API tag 双核） | BAAI/bge-large-zh-v1.5，MIT | BAAI/bge-reranker-v2-m3，Apache-2.0 |
| 固定 revision | 5617a9f…（镜像 API 取得并用于 URL） | 79e7739… | 953dc6f… |
| 获取来源 | **hf-mirror.com（镜像）**——huggingface.co 直连 DNS 污染不可达；镜像≠上游作者渠道，已如实区分 | 同左 | 同左 |
| manifest v2 现verify | **OK**（10 文件/2293MB） | OK（10/1303MB） | OK（6/2293MB，model_id 修正过=正确） |
| 下载时逐文件 SHA256 vs 当前 manifest | 9/9 非权重文件逐字节一致；权重为转换后新文件 | 同左 | 6/6 全部一致（原生 safetensors 未转换） |
| bin→safetensors 转换链 | 输入 bin SHA=b5e0ce34…（下载时在案）；**bin 已删**→转换不可独立复演（见 GAPS-1） | 输入 bf84a56f…（同左） | 不适用 |
| verify 能发现缺文件/多余/字节漂移/哈希漂移/维度不符 | MF1-M6 测试 + 维度不符集成测试覆盖（exit 3 / fail-closed / dimension_mismatch） | 同左 | 同左（供应链侧） |
| 评估文件=锁定文件 | sidecar 启动 verify（fail-closed 退出）+ console pin 比对（门禁 P-* 双 PASS） | 同左 | 同左（harness --manifest 门先行） |
| 本地 SHA256 ≠ 上游签名/attestation | **确认未等同**：披露端点显式 external_attestation=NOT_CONFIGURED；本地 manifest 自洽不冒充外部背书 | 同左 | 同左 |

## 第三步：指标复核（全部由原始 JSON 复算）

- **全集门禁复算 = 报告逐位一致**：双模型 R@5=25/26=0.9615（direct 4/4、同义 8/8、
  跨表述 8/8、混合 5/6）、empty=7/8=0.875、P50/P95=169/361 与 174/280ms、引用=回溯=1.0
  （per_query/empty_detail 原始行复算）；
- **数据集/分母/配置口径**：矩阵=calib(13QA/4空) 选择、holdout(13/4) 只验证；全集门禁
  (26/8) 走 console 全链且注入矩阵校准配置（m3:{w.7,idf,f.35}，zh:{w.7,plain,f.30}）——
  三套数字分属三个数据集，报告均已分别标注，**无混用**；
- **表间差异解释**（全部可由原始证据解释）：calib 1.0 vs holdout 0.9231 vs 全集 0.9615
  =不同数据集；holdout empty 0.75(3/4) vs 全集 0.875(7/8)=空查询样本不同；矩阵无 HTTP
  延迟（预嵌入离线打分）vs 门禁 P50=全链 HTTP（含 sidecar 往返，127.0.0.1，Docker
  Desktop 15.6GiB VM、CPU-only）——计时范围不同，报告引用无交叉；
- **reranker**：e5 提升（0.6154→0.9231 calib）与"对新模型无增益"（1.0→0.9231）均与
  matrix.json 一致；每对成本真实值由 seconds 字段复算=1440/2210/2634ms——
  **JSON 字段 per_pair_ms 单位错误（×1000）**，报告数字为复算正确值（口径问题已登记，
  非数据错误）；
- **未核实项**：无（全部数字均溯源到原始 JSON；无自行补推）。

## 第四步：准入边界检查

1. **TRIAL_READY**：eval2/zh-gate/审查报告全部显式维持 ✓；
2. **未进生产配置**：bge-m3/zh 零出现在 promote3（实测）/fxv-pilot 发行物 compose（grep
   空）/HYBRID_CONFIGS 默认值（实测=旧三档未变）；评估配置仅存在于隔离栈 env ✓；
3. **reference-only**：policy-check finding/ticket/gate/VERIFIED 全拒 + Fixer 拒 RAG-only +
   Verifier 只认 harness/test——门禁 BOUND 双模型实测 PASS，review.mjs 未变 ✓；
4. **C 链 BLOCKED/off**：promote3 compose enforce=0 + 两份并行文档 BLOCKED；zh-eval 栈
   无 keystore env、attestation 未配置（NOT_CONFIGURED 如实）✓；
5. **F1 与 eval2 未混接**：独立谱系（零共同提交）；合并时需人工调和 store.mjs（F1 +74 行
   vs eval2 未改）——登记为集成注意项，非冲突。

## 第五步：结论分档

**EVIDENCE_VERIFIED**——可进入单独授权的模型准入/集成计划（≠生产 READY）。准入前必补：

| # | 缺口 | 性质 |
| --- | --- | --- |
| G-1 | **bin→safetensors 转换不可独立复演**（bin 已删；输入哈希在案但转换脚本为会话内一次性）。准入流程应：官方渠道重新受控入库（admission list 既定路线），或由 bin 哈希重新下载复演转换并比对 safetensors 哈希 | 供应链留痕 |
| G-2 | **镜像信任**：hf-mirror 与上游同源未获独立验证（huggingface.co 不可达）；正式入库必须官方渠道+上游文件哈希交叉核 | 来源验证 |
| G-3 | rerank_timing.per_pair_ms 单位错误（×1000）——下游引用需以 seconds 复算（本报告已给正确值） | 证据字段缺陷 |
| G-4 | promote3 相关报告 09:57Z 后存在陈旧陈述（本报告已以实测调和；后续报告以 b2e8727d 为基线） | 口径纪律 |
| G-5 | F1（PR #243）与 eval2 的 store.mjs 合并调和（F1 的 DF 失败可观测性应保留于任何后续集成） | 集成前置 |

**REPORT_CONFLICT：无**（promote3 口径已用三份文档+镜像创建时间调和为时间差+陈旧陈述）。
RAG 状态：**TRIAL_READY 维持**；不得因此接入 promote3 或启用 C 链。
