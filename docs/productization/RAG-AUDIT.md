# RAG 全链路审计（RAG-AUDIT）

**日期**：2026-09-22（第二轮）｜ **方法**：沿真实链路逐节点取证（代码位置 / 运行配置 / 激活日志 / 调用审计 / 消费证据），不用目录名或注释推断。
**状态标记**：已实现（代码在）／已接线（配置+启动路径接通）／已运行验证（有真实运行证据）／历史证据（当时真、现状未复核）／未验证／缺失。

## 0. 一句话结论

RAG 在本系统里是**已接线、有真实运行与消费证据、但当前服务未启动且与版本/失败语义脱节**的辅助知识链：worker 内 MCP 工具 `rag_retrieve` → 宿主机 rag-live（BM25 词法检索、组织安全标准语料、12 chunks）→ 结果带 document/chunk/source_ref 进入 agent 上下文 → 历史结论**实际引用过**来源。缺的是：语料事实源不在 repo、无快照版本绑定（manifest rag.version=null）、失败语义未产品化（RAG 挂掉=静默无检索）、审计 JSONL 无 run_id 关联。这些是本轮补齐对象。

## 1. 实际是什么、服务于什么决策

两条独立知识链（用途不混）：

| 链 | 工具 | 内容 | 用途 | 后端 |
|---|---|---|---|---|
| **A 知识链（本审计主对象）** | `rag_retrieve`（MCP） | 组织安全标准/知识（CWE 定义、路径包含规范、命令执行规范、FastAPI 安全清单、审查输出规范 + 2 个演示文档），**平台自有合成资料，无企业数据、无案例结论、无仓库事实** | 审查时给 reviewer/fixer 提供"组织标准参考"（advisory） | rag-live（Node BM25 词法，`lexical-zh-en-v1`） |
| **B 案例链** | `skill_case_retrieval`（Skill MCP） | 历史审查案例（elemiso-case-pg `knowledge` 表，7 行，repo_scope 全部 = nghqqa/fastapi-boilerplate-demo，含 source_version/embedding_model 列） | 相似历史案例参考 | PG + pgvector 形状（本地 case-pg 在运行） |

**仓库代码检索不是 RAG**：reviewer 在工作区直接 clone + git diff（kickoff SPEC 步骤 2），与本审计无关。
依据（语料 disclosure 原文 + 审计记录 data_mode）判定两链用途边界清晰，不属"实质改变产品范围的歧义"，**不提请用户决策**。

## 2. 链路逐节点（A 知识链）

| # | 节点 | 现状 | 证据 |
|---|---|---|---|
| 1 | 知识来源 | **已运行验证（历史）** | `r3work/rag-live/rag-live-corpus.json`：8 文档 12 chunks，`data_mode=SYNTHETIC`（平台自有合成=可公开），disclosure 明确"无案例/无仓库事实/无预期结论"。**缺失：语料文件不在 repo（事实源旁落 r3work）；无 snapshot_id/更新时间字段** |
| 2 | 入库/切分 | **缺失（手工一次性）** | chunks 是手写 JSON（无导入管道）。本轮补：repo 语料事实源 + 可重复导入工具 + 内容寻址 snapshot_id |
| 3 | 索引/检索 | **已运行验证** | `r3work/rag-live/rag-live-server.mjs`：零依赖 Node≥18，BM25（IDF+TF+CJK bigram），启动即从 corpus JSON 构建 DF 表。**无 embedding、无外部付费调用**（语料本地、检索本地——无隐私外发面） |
| 4 | 检索接口 | **已运行验证（历史）** | `GET /api/rag/search?q&k` 返回 query_hash/top_k/data_mode/results[]{document_id,chunk_id,score,source_ref}；`/health`；`POST /api/rag/toolspan-audit`；`GET /api/rag/toolspans`。**当前 4174/4184 无监听（netstat 实测）=服务未运行** |
| 5 | 工具注册 | **已接线（当前镜像在位）** | worker 内 `/usr/local/bin/rag-mcp-server.mjs` 存在；`/etc/agentloop-rag.json` `enabled:true` → `:4184`；`zz_agentloop_rag.py` .pth watcher patch `CoPawAgent.register_mcp_clients` 注入 `rag_mcp` stdio 客户端（**对全部角色生效**） |
| 6 | Agent 实际调用 | **已运行验证（历史）** | hook 日志最近激活 **2026-09-21**（"rag_mcp connected and injected, endpoint=:4184"）；审计 JSONL 140 条中 `rag.retrieve` **64 条（58 OK + 6 EMPTY）**，最新 2026-09-19，延迟 1-14ms |
| 7 | 结果进入上下文 | **已运行验证（历史，消费侧引用为证）** | MCP server 返回 citation_rule（"answers must cite source_refs; uncited answers are UNVERIFIED"）；`evidence/rag-minio/elemiso-pr2rag-gate/result.md` 中 reviewer **实际引用** `org-standards/cwe-22-path-traversal.md#1`、`org-standards/file-path-containment.md#1`（带"references only"限定） |
| 8 | 结论引用来源 | **部分** | 引用格式=source_ref（文档#chunk），可人工核对；但**结论与 run 的关联靠 result.md 中的 run 标识+时间窗推断，审计 JSONL 无 run_id 字段**（只有 query_hash+ts）。深度修复=运行时变更，列入授权请求 |
| 9 | 失败语义 | **缺失（静默降级）** | rag-live 未运行时：MCP 调用连接失败 → 工具报错 → agent 看到 error（kickoff 说"如首次报错等 10s 重试一次"→再失败即无 RAG 继续）→ **结论不会标降级，投递照常 PROCESSED**。当前产品语义=RAG optional，但降级**不可见**，不满足 RAG-6 |
| 10 | manifest/版本绑定 | **缺失（已在本轮补）** | manifest `rag.version=null`；无快照哈希绑定。RAG-4 要求派发前固定快照——本轮实现 |

## 3. 链路逐节点（B 案例链，简）

- Skill 在 worker 镜像 `/opt/mergepilot/skills/case_retrieval`（已实现）；DSN 经 `agentloop-skills.json` 注入（case-pg 专用凭证）。
- case-pg **在运行**，`knowledge` 表 7 行、单 repo_scope、含 source_version/embedding_model/embedding_version/adopted 列（结构满足追溯）。
- 审计记录：`skill_case_retrieval` 8 次 OK 但 **document_count 全 0**——调用成功、零命中（要么查询不匹配、要么库当时为空）。**未验证**：非零命中的真实行为。
- 判定：案例链 = 已接线 + 调用证据（零命中），**历史案例对当前 commit 的覆盖能力未验证** → V0 RAG 验收以 A 链为主场景，B 链列为补充（证据要求同标准时另立项）。

## 4. 审计问题清单逐答（提示词四.1-10 关键项）

1. **代码在哪/入口**：服务=r3work/rag-live/rag-live-server.mjs（宿主机 node 启动，日志证实 4184）；worker 侧=镜像内 rag-mcp-server.mjs + zz_agentloop_rag.py hook。repo 内 `tools/rag/rag_retrieval_service.py`（M6 Python 服务，包装 case_retrieval）**不在当前 live 链**——只有 tests/rag、tests/m7_rag_benchmark 引用，属历史/测试资产，勿混淆。
2. **演示 vs 实际**：`release/agentloop-copaw-image/rag/rag-mcp-server.mjs` 注释自称"SYNTHETIC demo RAG"且默认端口 4174——但这是**同一文件的运行时配置差异**（RAG_ENDPOINT 环境驱动，worker 实际配置指向 4184 的 knowledge corpus）；"SYNTHETIC"是数据来源声明（平台自有、无企业数据），**不是 mock**：检索是真实 BM25，结果被真实引用。
3. **调用条件**：每个 agent 构造时注入 rag_mcp；调用与否由 agent 自主决定（kickoff 提示词引导"任何代码 diff 必须 skill_diff_parse，确认 finding 时查 rag_retrieve"）。
4. **运行配置**：worker 配置 enabled；**服务端当前未运行**（净状态：若此刻派发，RAG=不可用且不可见降级）。
5. **知识更新/版本**：当前无版本机制（本轮补 snapshot_id）。
6. **查询范围**：A 链是全局组织标准，**无仓库范围概念也无须要**（内容无仓库事实）；B 链有 repo_scope 列且当前单仓库。跨作用域误检索风险：A 链无（内容不分级）；若未来 A 链加入仓库专属内容，必须引入 scope——记录为规则。
7-8. 见节点 7/8。
9. **失败处理**：见节点 9（缺口）。
10. **真实调用证据**：64 条 rag.retrieve（58 OK）+ result.md 引用 + hook 激活日志；对应代码=上述文件、语料=r3work corpus（12 chunks，与 /health 日志一致）。

## 5. 缺口 → 本轮修复映射

| 缺口 | V0 条目 | 修复 | 层级 |
|---|---|---|---|
| 语料事实源不在 repo、无导入入口、无版本 | RAG-3/4、A | `tools/rag/corpus/`（事实源副本）+ `corpus_tool.py`（validate/import/snapshot_id 内容寻址、幂等） | 单测+隔离集成 |
| manifest rag.version=null | RAG-4 | 桥派发前读语料→快照 sha256+chunks+data_mode 入 manifest；`/health` 探测记录 dispatch 时服务状态 | 单测 |
| 失败静默降级 | RAG-6 | `MERGEPILOT_RAG_REQUIRED=1` 时派发前快照不可读或服务不可达 → ERROR(RAG_REQUIRED_UNAVAILABLE) fail-closed；默认 0=现状 advisory | 单测 |
| 服务真实行为无本地确定性测试 | RAG-1/2/6/7 | tests/rag_live/：**启动真实 rag-live-server.mjs**（随机端口+测试语料）做已知命中/合法空/快照稳定/不可达/注入语料返回形状测试 | 隔离集成（第 2 层） |
| 审计无 run_id | RAG-3 | 运行时变更→不擅改；记录关联方法（manifest 时间窗+query_hash+result.md run 标识）+列入授权请求 R5 扩展 | 文档 |

**第 3 层（模拟模型调用链）与第 4 层（真实模型端到端）状态**：第 3 层=历史证据存在（真实 worker→真实检索→真实引用，2026-09-17~19），**当前代码版本未复跑**；第 4 层=需 R1/R2/R4 授权。**本轮完成后 A 链的第 1、2 层验证齐备，第 3/4 层待授权，不冒充。**
