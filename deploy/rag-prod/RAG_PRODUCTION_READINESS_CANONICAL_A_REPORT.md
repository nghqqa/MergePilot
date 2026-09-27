# RAG_PRODUCTION_READINESS_CANONICAL_A_REPORT

日期 2026-09-27。**最终状态：RAG 维持 TRIAL_READY**（真实外部依赖与目标语言评估
均未满足，见 §5/§6——本波为代码整合与选择性补强，不构成升级条件）。

## 1. 实际整合基线与提交

- **远端核实**：git smart-http 443 中断期间改用 gh API 实测
  `repos/nghqqa/MergePilot/branches/main` → **bc44e637743c4d0e37fbc35a9a32e442b96d31e3**
  （与预期一致，无演进差异；A/B 均为其直接后代；两 worktree 干净，B 会话已收工）。
- **整合分支**：`feat/rag-canonical-a`（worktree `D:\goai\mp-worktrees\rag-canonical-a`，**本地未 push**）：
  - `ad635c9` merge: 候选 A（4340166）作为唯一代码基准（--no-ff 留审计；main 未演进故零冲突裁决项）
  - `dc6126a` 自 B 选择性补强（scope/披露/manifest 加固 + 新测试）
- 未合并 B 的 jobs/schema/embed/API，无 cherry-pick；候选 A/B 分支零改动；
  canonical promote3 未触碰（dec398d healthy、enforce=0）。

## 2. 保留与吸收

**从 A 全量保留**：真实语义链（bge-large-en-v1.5 numpy 运行时+attested sidecar+manifest
pin）、多维度 schema（models.dims+chunks_semantic 1024）、持久 jobs 队列（queue.mjs：
内容寻址幂等/心跳/退避/死信复活/陈旧重占）、机器端点（verifyRunBindingAndAudit 复用）、
VERIFIED 晋升黑名单、eval 栈/compose/语料/基准 harness/e2e。

**从 B 选择性吸收（重实现，非 cherry-pick）**：
1. **scope allowlist（并加固超出 B）**：`RAGTRIAL_ALLOWED_SCOPES`——**未设置=默认全拒**
   （B 有缺省回退允许已知 scope，违反"缺配置不得扩大范围"，已纠正）；覆盖
   /query + /machine/query（B 漏了机器通道）+ A 链内部查询三路同权；403+
   QUERY_SCOPE_DENIED **有界脱敏审计**（同源窗口封顶，只记 repo@branch+原因，不落查询文本
   不回显 allowlist）；env 撤销即时生效；机器身份验签通过仍受 scope 约束。
2. **供应链披露**：`/api/rag-trial/providers` + status 组件——provider 类型/model_id/
   manifest_sha256/dims/runtime/验证方式；state 用 **LOCAL_MANIFEST_VERIFIED** 并显式
   `external_attestation: NOT_CONFIGURED`（本地 manifest 验证不冒充外部 attestation 成功）；
   不回显密钥/keystore 路径/env 原值。
3. **manifest v2 三重校验（B 概念扩展）**：`files[{name,sha256,bytes}]`+files_count+
   total_bytes——**文件数/每文件字节数/sha256 三重 + 总字节 + 必需工件强制**
   （权重/tokenizer/config 任一缺失 fail-closed）+ 夹带多余受管文件=漂移；
   console 侧结构完整性 fail-closed；新 manifest pin `05c8959d…`（sidecar/compose/env 同步）。

## 3. 本轮验证（全部实际执行，证据可溯）

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| canonical 集成（scope 矩阵 SC1-7/轮换 KR1-2/manifest MF1-6/双 worker WC1-2/披露 D1） | **18/18** | 本轮 console 输出（SC4 撤销即时、SC5/SC7 机器越权+审计封顶、MF3 字节漂移、WC1 双 worker 6 任务恰一次 attempts=6） |
| 栈 e2e（+M6 机器 scope 越权） | **19/19** | `evidence/rag-prod/2026-09-27T04-55-32-931Z/`（summary/transcript） |
| prod/pg 集成 + 单测 | 17/17、25/25、25/25 | 本轮输出 |
| 后端全量 | **157/0（两次）** | 本轮输出（一次瞬时 flake 未复现） |
| 前端构建 | ✓ | `npm run build` dist 产出 |
| secret-scan --path . | **0 命中** | 本轮输出（测试口令改豁免形状 canontestpassword） |
| HMAC 矩阵 | M1-M6 | e2e：有效/坏签名/重放/时间窗/无 keystore BLOCKED/机器越权 403 |
| keystore 轮换/撤销 | KR1-2 | 集成：撤销旧 key→401，新 key→200 |
| 崩溃恢复/重试/死信 | W4/W5+集成 | 陈旧重占 JOB_RECLAIM；provider 故障→重试成功 |

## 4. 语言与质量边界（整合分支实测）

**英文评估集**（8 文档/20 QA/6 空查询，生产代码路径，本轮重跑
`evidence/rag-prod/2026-09-27T04-54-50-846Z/benchmark.json`）：

| 指标 | local-hash-v1（deterministic 基线） | bge-large-en-v1.5 |
| --- | --- | --- |
| Recall@1 / @5 | 0.90 / 1.00 | **0.95 / 1.00** |
| MRR@10 / NDCG@10 | 0.9417 / 0.9565 | **0.9750 / 0.9815** |
| 引用命中率 | 1.00 | 1.00 |
| 空结果准确率 | 0.1667 | **1.0000** |
| P50 / P95 | 7 / 14 ms | 128 / 377 ms |

**中文：未达标，不得外推**——bge-large-en 的 3 组 zh↔en 平行对同义相似度
0.43/0.47/0.52 全部低于无关对 0.63/0.77/0.78（系统性反向错排；词表仅 488 CJK 单字）。
英文指标只按英文评估集陈述。`local-hash-v1` 保留为 deterministic 测试 provider，
披露端点显式标注"不宣称真实语义能力"。未下载/分发任何新模型。

## 5. RAG 仍为 TRIAL_READY 的原因

1. **中文语义未达标**（§4）——生产主语言能力缺失；
2. **外部 attestation 服务未接入**（MERGEPILOT_PROVIDER_ATTEST_URL 空；本地 manifest
   验证≠外部 attestation，披露如实 NOT_CONFIGURED）；
3. **生产 keystore 分发未执行**（评估栈为合成密钥；轮换/撤销机制已验证但真实分发
   需授权）；
4. **canonical promote3 未切换**（按禁令维持 dec398d；发行物化/双回滚锚点演练待授权）；
5. C 链三输入未齐（model cache/provider attestation/keystore 分发），enforcement 保持 off。

## 6. 进入生产所需（后续授权清单）

1. **模型授权**：zh/多语语义模型（明确来源/许可/SHA256 manifest，受控离线分发）
   → 以现有 benchmark harness 在中文评估集重测（门槛：zh Recall@5 与空准确达标）；
2. **keystore 生产分发**：真实 RUN_BINDING 密钥经受控通道写入 keystore 目录
   （轮换经 /api/cchain/keystore/rotate，撤销经 revoked 标记——机制已测）；
3. **staging 试点**：canonical promote3 发行物化本分支（新镜像+双回滚锚点）+
   受控试点流量（单独授权波）；
4. **人工授权**：分支 push/PR/合并审批；C 链三输入齐备后 enforce 决策另行授权。

## 7. 禁令遵守声明

未 push/建 PR/评论/approve/merge；promote3 未修改未重建；未分发真实 keystore
（合成评估密钥 gitignored）；C 链 enforcement 未启用；未新增用户/仓库/PR/真实流量；
未创建 latest；未读取/输出/提交真实凭据；**RAG 未标记 PRODUCTION_READY（TRIAL_READY）**。
