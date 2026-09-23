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


## 6. 授权前最终复核（2026-09-24 只读轮，基线 f538141）

| 复核项 | 结果 |
|---|---|
| PR #2 head/分支 | head=`65de83d6`（=分支 tip，未变，已 PROCESSED→**需新 head**）；源分支 `demo/high-risk-human-gate`；**目标分支=`mergepilot-demo/schema-migration-risk`（非 main），mergeable_state=dirty**（与 base 有冲突；审查用 merge-base diff，不受影响，但结论预期含冲突说明） |
| 定向认领与副本 | `MERGEPILOT_TARGET_REPO/PR/HEAD` 三项全设才过滤、缺一 fail-closed（gh_bridge.py target_filter_sql）；运行副本 repo==r3work 逐字节一致（4408bc58/a1745c52） |
| 模型 | reviewer/leader active_model 均为 deepseek-flash（本轮重读回确认） |
| BM25/rag-live | 语料 6989B/12 chunks 在位；server 在位（RAG_LIVE_PORT=4184）；当前 :4184 **已停止**；启动=`RAG_LIVE_PORT=4184 node rag-live-server.mjs`（cwd=r3work/rag-live），health=GET /health；桥 rag 门默认 advisory（不可达照常派发，状态入 manifest），但**采集检索证据必须案例窗口内启动** |
| 截止/停止/恢复 | `--timeout-min 20`；超时→conclude(timeout)→**仍会发布 1 个 neutral check-run**→delivery=ERROR TIMEOUT(manual)。停止顺序：①桥进程停止（delivery 行保持 RUNNING，45min 后下轮桥 take_over_stale 自动接管，resume 按 receipt/项目权威状态续接，不重发 kickoff）；②`agt worker sleep --name reviewer/leader`（或等空闲自停）；③停 rag-live node 进程。**任何停止都不取消已发出的 provider 请求** |
| reconcile/单 check-run | 发布顺序=本地回执→GitHub 对账（RECORD_MATCH 采纳/UNATTRIBUTED 人工/NO_MATCH 才 POST）→POST→回执落盘；有界 3 次退避；传输失败=unknown 禁止盲重发；**每 run 至多一个 check-run**（采纳路径不重发）。新 head 对账预期 NO_MATCH（干净 POST） |
| provider 硬上限 | **无**。链路无消费上限配置（AGENTTEAMS_MODEL_MAX_TOKENS=8000 仅为单请求 token 上限，非费用上限）；DeepSeek 平台为预付费余额，余额耗尽才隐式断供。**预计费用不是硬上限；在途请求可能继续计费；停止操作不保证取消已发出的请求** |

## 7. CASE2 专属逐项授权表（逐项独立批 ⌈批/不批⌉）

| # | 授权项 | 内容 | 外部影响 | 状态 |
|---|---|---|---|---|
| A1 | **空提交** | 向 `demo/high-risk-human-gate` push 一个空提交（产生新 head，回填验收表） | 1 个新 commit 对象+分支引用前移；触发 webhook | ☐ 待批 |
| A2 | **真实审查** | leader/reviewer 以 deepseek-flash 执行一次（20min 截止；fixer/verifier 关、v3 shadow、不自动修复、不 merge） | 付费模型调用；**无 provider 硬上限：在途请求可能继续计费，停止不保证取消已发出请求** | ☐ 待批 |
| A3 | **rag-live** | 案例窗口内启动 :4184，窗口结束即停 | 仅本机进程；审计追加 JSONL | ☐ 待批 |
| A4 | **check-run** | reconcile-first 后至多发布 1 个 mergepilot/review（HIGH 预期→action_required，随后停人工门等操作员决策） | 1 个 check-run 对象 | ☐ 待批 |
| A5 | **费用风险接受** | 无 provider 硬上限时，是否接受 20min 截止+人工停止+**在途费用风险**（停止≠取消；费用可见性暂缺，事后对账） | 账务风险 | ☐ 待批 |

约束重申：单仓库/单 PR/单 head/单 run；PR #9 既有授权与 PAT 凭证**不**构成 CASE2 授权；
审查自然调用 rag_retrieve→记录 BM25 模式与审计证据；未调用→如实报零消费，不强 Call、不改结论。
