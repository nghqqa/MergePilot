# CASE2 候选与验收准备（2026-09-23 只读筛选轮；基线 74a3c50）

**性质**：只读筛选与准备。本轮零 GitHub 写入、零模型调用、零基础设施变更。
执行需下一轮的精确授权（见 §4）。

## 1. 事实基线（本轮核实）

- **模型**：reviewer/leader 实际生效 = `deepseek-flash`（openclaw primary + active_model 双读回一致）；
  上游目录仍为 deepseek-flash / deepseek-v4-pro。代码/桥默认值仍 deepseek-chat（未动）。
- **CASE1 结论**：真实审查成立（leader+reviewer 真实调用、skill_diff_parse+skill_sast_scan 各 1 次），
  **rag_retrieve 零调用**（PASS/NOT_CONFIRMED，无 finding → 无检索动因，符合规范路径，如实记录）。
- **run_context 同步**：`tools/gh-bridge/{gh_bridge,run_context}.py` 与 r3work 运行副本
  逐字节一致（4408bc58… / a1745c52…）。下一真实 run 即产生可信审计边界。
- **RAG 模式**：现行唯一有效能力 = BM25 词法检索（lexical-zh-en-v1，12 chunks，SYNTHETIC，
  knowledge-only）。**D-7 未批准**（skill-rag 分支 HEAD `d4c2733` 核实：外部 embedding
  数据外发默认不允许、向量分支不启用）——本轮评估仅按 BM25，未下载 embedding、未外发代码。
- **语料主题**（决定自然触发场景）：CWE-22 路径穿越定义、文件路径包含性校验组织标准
  （realpath+commonpath+400/404 约定）、CWE-78 命令注入定义、命令执行组织标准
  （禁 shell=True）、FastAPI 端点安全清单、审查输出协议、rag 使用政策、运行时可观测事实。
- **测试债务**：维持 74a3c50 登记——84 failed + 21 setup errors，全部在环境/产物依赖套件
  （TEST-DEBT.md），不称全绿、不放宽断言；本轮门禁=改动面相关目录绿（本轮无代码改动）。

## 2. 候选清单（repo 均为 nghqqa/fastapi-boilerplate-demo）

| PR | 分支 / 当前 head | 候选理由 | 预期检索场景 | 需新提交 | 预期模型费用 | check-run 写入 |
|---|---|---|---|---|---|---|
| **#2**（首选） | demo/high-risk-human-gate @ `65de83d6`（已 PROCESSED，check 105874262425 success） | `demo_download(name)` 用户输入直 join 基目录无包含校验 = 故意植入的 CWE-22 任意文件读；reviewer 大概率 CONFIRM HIGH → kickoff 规范明确"确认 finding 后查 rag_retrieve 匹配组织标准"→ **自然触发** | doc-cwe22-def + doc-path-containment（含 400/404 响应约定） | **是**（当前 head 已处理，需空提交产生新 head） | 估算：diff +122 行（约 CASE1 的 4 倍），leader+reviewer 会话 token 量级 10⁵ 以内；flash 档单次 << $1。**无 provider 计量，准确值未知**；预算=20min 人工截止 | 1 个（预期 conclusion=action_required，HIGH 停人工门；无 fix 派发） |
| #3（备选） | demo/high-risk-human-reject @ `03312d65`（已 PROCESSED，check 105875453297 failure） | `demo_cmd_exec` 用户可控 host 拼命令 = 故意植入 CWE-78；同样符合"确认→查标准"触发路径 | doc-cwe78-def + doc-cmdexec-standard | 是（同上） | 同上量级 | 1 个（历史路径=rejected；本轮范围 fixer/verifier 关，仅审查） |
| #1 / #5（对照，不推荐） | 各自 head 已 PROCESSED | 迁移唯一性改造，合法改动 | 语料无迁移/DB 标准 → 预计零调用或合法空结果（可作"零调用如实记录"对照） | 是 | 更低 | 1 个（预期 success） |
| #11/#12/#13（对照） | 各自 head 已 PROCESSED | 小工具函数（env 校验/掩码/时长格式化） | 无对应语料 → 预计零调用 | 是 | 最低 | 1 个（预期 success） |

注：**所有当前 head 均已 PROCESSED**（桥 already_processed 去重会跳过重跑）→
任何重跑都必须先产生新 head（空提交 push 属 GitHub 写入，需授权）。

## 3. 选定案例验收表（PR #2，执行时逐格填写）

目标链路：真实审查 → 自然触发 rag_retrieve → BM25/lexical-zh-en-v1 → run_context →
manifest/head 关联 → 审计记录。

| 字段 | 来源（可信层级） | 执行后填值 |
|---|---|---|
| run_id | 桥生成（manifest/run-context） | 待执行 |
| attempt_no | 桥计数（投递行 RQn+1） | 待执行 |
| repo / PR / head | 投递行+manifest（三方一致才算过） | 待执行 |
| manifest_id | manifest 规范 sha256（kickoff 引用行同值） | 待执行 |
| skill_digest | manifest skills.content_sha256 | 待执行 |
| retrieval_mode | manifest rag.retrieval_mode（lexical-zh-en-v1） | 待执行 |
| rag_retrieve 调用与否 | rag-live toolspan 审计（result_status/document_count/latency/source_refs） | 待执行 |
| document_count / source_refs / 错误码 | 同上 | 待执行 |
| model（派发侧/响应侧） | manifest.model.primary + catalog_state_at_dispatch；响应侧暂无回传 | 待执行（响应侧=验收限制） |
| **验收限制（不伪装已打通）** | ① rag_retrieve **call_id 不入现行审计**——归属用窗口相关回放（单飞前提）；② RAG 权威审计仍在 :4184 JSONL，**PG audit 域（P3）未实现**；③ 后端事件（run_events）与桥执行未关联 | — |

判定规则：reviewer 判定无需检索 → 如实记录零调用，**不算成功消费**，不改结论、不强 Call。
成功消费 = 审计含 rag.retrieve 记录 + 结果被 reviewer 在 findings 中引用组织标准内容（间接佐证），
且全程无人工强迫。

## 4. 执行 PR #2 案例需要的精确授权清单

1. **空提交授权**：对 `demo/high-risk-human-gate` push 一个空提交产生新 head
   （明确的 GitHub 写入；push 后 head SHA 回填上表）。
2. **模型调用授权**：leader+reviewer 以 deepseek-flash 真实执行（付费）；预算=20min
   人工截止，fixer/verifier 不派发，v3 维持 shadow。
3. **check-run 授权**：允许桥在新 head 上发布**至多一个** mergepilot/review
   （HIGH 预期 → action_required；随后停在人工门，等待操作员决策，不自动修复/不 merge）。
4. **rag-live 启动授权**：执行窗口内启动 :4184（advisory 现状下也可不启动——但为采集
   审计必须启动；结束即停）。
5. 明确**不授权**项：故障注入、共享 case-pg/语料/凭证变更、merge、第二并发 PR。

## 5. 失败处理矩阵（沿用既定口径）

provider 401→auth/manual；429/5xx→retryable 有界重试；传输未知→PUBLISH_UNKNOWN 先对账；
head 变化→stale 停止；预算不足→不发新 attempt；manifest 冲突→fail-closed 不删旧；
审计缺失→记接线失败，不得把模型调用标为 RAG 成功；外部副作用不确定→人工处理。
