# RAG_ZH_PRODUCTION_CANDIDATE_PREPARATION_REPORT

日期 2026-09-27。分支 `feat/rag-zh-gate`（基线 canonical-a@7c3775b，上一提交
1ec843b，本波新增提交见 git log；本地未 push）。**结论：未出现合格中文生产候选；
RAG 维持 TRIAL_READY。** 检索评分体系已升级为可校准的混合评分 v2 并以
calibration/holdout 协议完成一轮完整评估——证明当前瓶颈是**模型能力界**，
不是评分/阈值可解。

## 1. 候选模型清单与授权需求

见 [MODEL_ADMISSION_LIST.md](MODEL_ADMISSION_LIST.md)。要点：
- 本地缓存 36 模型中**无合格中文/多语生产候选**（zh 门禁波已证）；
- 优先候选（公开规格，**待授权后以实际工件 manifest 为准**）：
  ① bge-m3（MIT，1024d，XLM-R SP 多语，~2.3GB）② multilingual-e5-large（MIT，
  1024d，与现有 e5 运行时同构，迁移成本最低）③ bge-large-zh-v1.5（MIT，中文专精）；
- 所需授权：来源/版本指定+分发许可 → 受控入库（生成 manifest v2）→ rag-zh-gate
  栈复跑门禁；清单不构成下载授权，本波零下载。

## 2. e5 与 bge 的失败原因（本波用校准前沿面定证）

**校准协议**：zh-gate-v1 分层切分 calibration/holdout（13+13 QA、4+4 空查询，
seed=20260927 入库）；网格 w∈{0.3..0.9}×lexMode{plain,**idf**}×floor{0.05..0.60}
（168 配置/模型）；选择规则预声明（空准确=1.0 且同义/跨表述≥0.5 前提下最大化
calib R@5）；holdout 仅验证不参与选择。证据：`evidence/rag-zh-gate/
2026-09-27T08-50-49-632Z/calibration.json`（含全网格+前沿面+四短名单 holdout）。

| 前沿面（空准确=1.0 约束下） | local-hash-v1 | bge-large-en | e5-base-v2 |
| --- | --- | --- | --- |
| 可行配置数 | 138 | 56 | 30 |
| 最高 R@5 | 0.5385 | 0.6154 | 0.6154 |
| 最高同义/跨表述 recall | 0.25/0.25 | 0.50/0.25 | 0.50/0.25 |

- **e5**：放开空准确约束可达 calib R@5 0.9231（同义 1.0/跨表述 0.75），但空准确
  崩至 0-0.25；约束空准确=1.0 则跨表述上限 0.25。**同一配置无法双满足——fluent
  中文偏题的语义分数与真命中的分数区间重叠，非地板/权重可分**（能力界）。
- **bge-en**：低地板下"召回恢复"（holdout R@5 0.9231）实为词法通道驱动
  （488 CJK 单字 WordPiece 语义近噪声），空准确随之崩（0-0.75）；英文模型
  zh 语义缺失的本质未变（zh 全集 R@5=0.31、同义/跨表述 0/8）。
- **holdout 验证**（反过拟合）：短名单四配置出样本复验——e.g. e5 max_recall
  配置 holdout 空 0.75 vs calib 0.25（空查询样本仅 4 条，单项摆动=0.25）——
  **证明小样本阈值调参不可信，必须按协议走 holdout**；所有模型 holdout 与
  calib 差距如实记录于 calibration.json。
- **IDF 词法**（语料驱动 DF，非停用词硬编码）已实现并入网格：在本语料
  （26 chunk）上未改变前沿面（DF 区间过窄）；对生产规模语料预期有效——
  接口已就绪（见 §3）。
- **reranker**：本地无任何 reranker 工件（缓存无 cross-encoder），未评估——
  纳入未来授权清单的可选项。

## 3. 混合检索改进落地（评分 v2，默认零行为变化）

- `store.mjs` 混合评分 v2：`final = w·vec + (1-w)·lex`，`lex ∈ {plain, idf}`
  （IDF=ln(1+N/(1+df))，DF 取 scope 全部活跃 chunk，进程内缓存 5min；
  生产大语料需持久化 DF 表——已按此设计预留接口并注明）；
- `HYBRID_CONFIGS` 按维度固定默认值=各模型部署现行值（256:{0.5,plain,0.1} /
  768:{0.5,plain,0.45} / 1024:{0.5,plain,0.52}）——**默认行为与 v1 逐字节一致**
  （栈上重跑门禁三模型指标完全相同）；
- `RAGTRIAL_HYBRID_JSON` 支持按 model_id/dims 覆盖（内部试验档位用），
  非法值回退安全默认（HYB1-4 测试锁死：默认值/IDF 路径/无覆盖一致性/非法回退）。

## 4. 生产门槛状态（未出现合格候选）

按预声明门槛（zh R@5≥0.90、同义/跨表述达标、空准确≥0.75、引用=回溯=1.0、
manifest v2、fail-closed 三件套、资源预算、scope/验签/审计全绿）：
- **bge-large-en**：zh 未达标（§2）——维持英文候选角色；
- **e5-base-v2**：zh 未达标（空准确能力界）——按任务定义**仅内部试验**，
  不进入生产配置（即使 holdout 单点数字好看也不破例——协议优先）；
- **local-hash-v1**：确定性基线（0.6154/0.75 为词法上限参照）；
- **结论：合格中文生产候选=无** → RAG 维持 TRIAL_READY。

## 5. 资源成本（本波实测，与上波一致量级）

sidecar 常驻 bge 180MB/e5 49MB、console 30MB、pg 53MB；索引 288-376KB/表；
e5 冷启 7.0s；worker 吞吐 10 任务<1s（local 模型）；P50：local 7ms / e5 59ms /
bge 177ms。均在本波部署预算内（预算声明见准入清单 §1.5）。

## 6. 生产依赖接口准备（只做接口与验证，未执行真实分发）

- **provider metadata/attestation**：/api/rag-trial/providers+status 披露已就绪
  （LOCAL_MANIFEST_VERIFIED vs external_attestation=NOT_CONFIGURED 严格区分）；
- **keystore 分发/轮换/撤销**：文件式 keystore+revoked/expires 语义、
  KR1-2 轮换撤销测试、cchain rotate 端点复用——流程文档化在
  PRODUCTION_RAG_READINESS_CANONICAL_A_REPORT §6，未分发真实密钥；
- **worker 常驻形态**：compose `resident-worker` profile 接口就绪
  （`docker compose --profile resident-worker up worker`；默认不启动）；
- **PG 备份/恢复**：pg_dump→临时 pgvector 容器恢复演练 **通过**（1395 行 dump，
  42 文档/3 模型/30 任务/语义表全量还原，RESTORE_DRILL_OK）；
- **队列死信/恢复**：死信+人工复活+陈旧重占（canonical 集成覆盖）；
- **缺件 BLOCKED 复核**：无 keystore→RUN_BINDING_AUTH_BLOCKED（M5）、
  无 manifest pin→BLOCKED、语义未接线→NOT_CONFIGURED（D1）——三态全部如实。

## 7. 验证边界（保持且本轮复验）

RAG 仅 reference（policy-check finding/ticket/gate/VERIFIED 全拒、Fixer 不接受
RAG-only、Verifier 只认 harness/test）——gate2 栈上实测 PASS；scope allowlist 403
（跨 repo/branch）实测 PASS；promote3=dec398d 未动；C 链 enforcement off/BLOCKED
未动；未 push/建 PR/merge；未创建 latest；未读取/输出/提交真实凭据；
**RAG 未标记 PRODUCTION_READY（TRIAL_READY 维持）**。

## 8. 回归与证据

- 套件：单测 25 + pg 集成 **29**（含 HYB1-4）+ prod 集成 17 + canonical 集成 18
  + 后端全量 **157/0** + secret-scan 0 命中；
- 栈：rag-zh-gate 六容器 healthy；门禁重跑（v2 默认配置）三模型指标与 v1 一致
  （零行为变化实证）；资源/冷启/吞吐/备份恢复演练证据
  `evidence/rag-zh-gate/2026-09-27T08-54-42-681Z/`。

## 9. 进入生产所需授权（不变+新增）

1. **zh/多语模型授权**（准入清单三候选之一或等价）→ 受控入库+manifest →
   复跑门禁（协议/阈值已固化）；
2. **keystore 生产分发**；
3. **external attestation 服务接入**；
4. **promote3 试点**（发行物化+双回滚锚点+受控流量）；
5. （可选）**reranker 工件授权**（若 dense+lex 前沿仍不足时的增量手段）。
