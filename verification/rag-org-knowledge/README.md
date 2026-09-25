# RAG_ORG_KNOWLEDGE_CHAIN_PREFLIGHT — A 链预检报告

基线：console @ a079367（未回退） · 隔离环境（本目录 + 一次性本地服务 127.0.0.1:4821x）
**状态维持：RAG registration=DISABLED · embedding 未下载未生成 · 未接入 skill_case_retrieval ·
未使用 pgvector · 无 GitHub 写入 · Fixer/Verifier 未启动 · 未访问共享 PG/MinIO · 范围未扩大**

## A 链组成

worker rag_retrieve →（隔离）rag-live 词法服务 → 组织安全标准语料（受控事实源）

- `corpus/org-security-standards.v1.json`：本轮撰写的通用安全标准语料；
  data_mode=org_knowledge、publication_status=published_internal；
  4 文档 / 10 chunk，每 chunk 携带 doc_id/title/source_ref
- `corpus-gate.mjs`：受控门禁——shape 校验 + 内容寻址（canonical 语料 sha256 →
  snapshot_id `snap-<32hex>` / corpus digest `<64hex>`）+ **禁含内容 screening**
  （仓库事实/run id/40hex SHA、密钥形态、个人数据、历史案例/CVE 引用——任一命中拒绝装载）
- `rag-live.mjs`：隔离词法检索服务（**lexical-zh-en-v1 固定版本**；CJK 二元组+拉丁词
  词法评分，零依赖）；每次检索写审计 JSONL
- `preflight.mjs`：预检矩阵（11/11）

## 预检矩阵结果（11/11 PASS）

| # | 验证 | 结果 |
|---|---|---|
| P1 | 语料装载 shape+screening；snapshot_id/digest 内容寻址 | ✓ snap-d903481f… |
| P2 | 检索版本固定 lexical-zh-en-v1 | ✓ |
| P3 | documents/chunks/source_ref/data_mode/publication_status 记录完备 | ✓ 4 文档/10 chunk |
| P4 | screening 负向：注入仓库事实 → 拒绝 | ✓ repo-fact 规则命中即拒 |
| P5 | 已知命中：密码/密钥标准 → std-001 | ✓ |
| P6 | 已知命中：高危人工门 → std-002 | ✓ |
| P7 | 合法空结果：零命中如实返回（非降级） | ✓ empty_reason=no_lexical_match |
| P8 | 服务不可达：显式不可达（worker 侧 degraded 前置事实） | ✓ |
| P9 | 审计关联：snapshot_id/query_hash/source_refs/service_state/run_id | ✓ 3 条预检驱动记录全字段 |
| P10 | 语料损坏（注入秘密形态）→ 拒绝装载，503 degraded 不伪装 | ✓ |
| P11 | 核心面：两授权 PR 只读路径不受影响 | ✓ POSTGRESQL_LIVE 双仓在位 |

## degraded 语义（要求 8）

- 语料损坏/被 screening 拒绝 → 服务启动但 503 + `service_state=degraded` +
  `degraded_reason=corpus_unavailable` + 响应头 `x-rag-service-state: degraded`，
  结果恒空——绝不以正常检索伪装
- 服务关闭 → 连接显式失败（调用方标记 degraded，无静默回退）
- 审计记录同步携带 service_state（ok/degraded 均落审计）

## 判定

**ORG_KNOWLEDGE_RAG_PREFLIGHT_READY**

（A 链自洽：词法检索无模型缓存/密钥分发依赖；前置轮阻塞项仅影响 C 链
skill_case_retrieval，不影响本链。C 链工件状态不变：RAG_ARTIFACTS_BLOCKED 三缺口仍在。）
