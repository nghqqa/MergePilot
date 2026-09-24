# 真实集成授权请求清单（INTEGRATION-AUTH-REQUESTS）

**日期**：2026-09-22 ｜ **状态**：等待用户逐项授权 ｜ **原则**：真实集成授权一次一清；未授权前不执行其中任何操作。
每项按提示词 §八 列明：目标环境与资源 / 操作 / 预期影响 / 验收证据 / 回退方法 / 所需权限。

## R1 桥故障注入（场景 1/2/4/8 实证）

- **目标**：本机桥进程（D:\goai\r3work\scripts\gh_bridge.py）+ 服务器 PG `mergepilot_audit` 库 `github_deliveries` 表（159.75.42.106，共享环境）。
- **操作**：注入打标测试投递行（delivery 事件字段带 `test-` 前缀、repo 用白名单内 nghqqa/fastapi-boilerplate-demo）；启动真桥；在 认领后/派发后/发布前 三阶段分别 `kill -9` 桥进程再重启，观察接管与恢复。**不杀共享 PostgreSQL 容器**（数据库故障属独立场景，另行论证）。
- **影响**：共享台账短暂出现 2-3 行测试 RUNNING/ERROR 行；本地栈拉起 4 worker 跑一轮审查（真实模型调用，走既有预算）；无对外写入（发布阶段单独由 R2 覆盖时可先阻断 reporter）。
- **验收证据**：接管日志（take_over_stale CAS rowcount=1）、resume 分流记录、RQn 计数、旧 claim rowcount=0；台账终态截图/导出。
- **回退**：测试行 `DELETE ... WHERE error LIKE 'test-%' OR delivery_id LIKE 'test-%'`；桥进程 kill 即消失；本地栈 `agt worker` 停止。
- **所需权限**：写共享 PG（已有 SSH 通道）、运行本地栈、真实模型调用额度。

## R2 真实 GitHub 回写 + 对账（场景 3/7 实证）

- **目标**：GitHub 仓库 nghqqa/fastapi-boilerplate-demo（V0 白名单内）+ 服务器 mp-checks-reporter 容器。
- **操作**：以测试分支 PR 为载体，跑完 桥→审查→reporter POST check-run 全链；人为制造 发布失败（断 reporter 网络/凭证只读化）后恢复，验证 reconcile 采纳与 receipt 短路。
- **影响**：GitHub 上产生可见 check-run（mergepilot/review）；App 配额消耗数次。
- **验收证据**：POST 201 + check_run_id、reconcile GET 采纳日志、receipt JSON、ERROR→恢复→PROCESSED 台账轨迹。
- **回退**：check-run 不可删除——使用专用测试分支（`mergepilot-test/*`），对主分支零影响；测试 PR 事后关闭。
- **所需权限**：已有 GitHub App installation（checks:write + metadata:read）；无需新权限。

## R3 场景 6 双 head 实证（PR 更新旧结论失效）

- **目标**：同 R2 仓库与测试 PR。
- **操作**：同一 PR 推送两个不同 head（两次 commit），触发两次投递，验证两个 head 各自的 check-run 并存、互不代答；辅以 `already_processed` 守卫日志。
- **影响**：2 个 check-run 可见；一轮重复审查成本。
- **验收证据**：两个 check-run 的 head_sha 各自正确；台账两行各自 PROCESSED。
- **回退**：同 R2。
- **所需权限**：向测试分支 push 的仓库写权限（用户操作或授权 agent 执行）。

## R4 运行副本同步 + 真实案例回归（部署操作）

- **目标**：repo `tools/gh-bridge/gh_bridge.py`（事实源）→ 运行副本 `D:\goai\r3work\scripts\gh_bridge.py`（startup_fullchain 启动的是它，DECISIONS #3）。
- **操作**：备份运行副本为 `gh_bridge.py.bak-20260922` → 复制 repo 版本过去 → 跑一轮真实案例（R1+R2 合并执行即可充当回归）→ 比对行为。
- **影响**：运行副本获得 M1-1/M1-2/M1-3 + run-manifest 全部加固；行为差异=加固本身。
- **验收证据**：副本 sha256 与 repo 一致；回归案例全链 PROCESSED + run-manifest.json 落盘（含 missing[] 诚实标注）。
- **回退**：`copy gh_bridge.py.bak-20260922 → gh_bridge.py`（一条命令，秒级）。
- **所需权限**：本机文件写入（无外部权限）；如需跑案例叠加 R1/R2 权限。

## R5 worker 侧版本上报（run-manifest missing 项补齐）

- **目标**：worker 镜像（zz_agentloop_otel hook 所在层）与 ctrl 容器。
- **操作**：只读探查（无副作用，可先行）：`agt` CLI 与 agentloop 运行时中模型标识/Skill 内容哈希/RAG 索引版本的可得位置；得出上报方案后再议镜像改动（改动属部署，另行授权）。
- **影响**：只读探查零影响；后续镜像改动需重启 worker（中断在途任务——需排期）。
- **验收证据**：探查记录；方案说明（记录于 DECISIONS 后再实施）。
- **回退**：只读阶段无需回退。
- **所需权限**：本机 docker exec（只读命令）。

## R6 token usage 源接入（成本计量 token/币值面）

- **目标**：外部 OTel collector（查询 API + 凭证）或 worker 本地 usage 台账（依赖 R5 的镜像层）。
- **操作**：二选一：a) 用既有 OTel key 查询 collector 的 genai.usage spans；b) worker 内落本地 usage 日志后由收集器读取。
- **影响**：a) 只读查询零写入；b) 同 R5 镜像改动。
- **验收证据**：单 run 的 input/output token 总数与 span 数自洽；costmeter 报告 tokens 不再标 unavailable。
- **回退**：a) 无；b) 同 R5。
- **所需权限**：a) OTel collector 查询权限（key 已有，存放见 secrets 备忘）；b) 同 R5。

## R7 语料部署同步（RAG 快照切换到运行环境）

- **目标**：repo `tools/rag/corpus/org-security-knowledge-v1.json`（事实源，snapshot fd34c304…）→ 运行语料 `D:\goai\r3work\rag-live\rag-live-corpus.json`；如服务侧有改动，`tools/rag/live/rag-live-server.mjs` → r3work 同名文件。
- **操作**：备份运行语料（.bak-<date>）→ corpus_tool.import（内容相同则零操作）→ 启动/重启 rag-live（当前未运行；启动本身属本项授权）→ 校验 /health 与 snapshot 一致。
- **影响**：检索内容切换到 repo 版本化语料；服务在宿主机监听 4184（仅本机回环面）。
- **验收证据**：/health chunks=12 + data_mode；桥 manifest rag.snapshot_id 与 corpus_tool 输出一致。
- **回退**：`copy rag-live-corpus.json.bak-<date> → rag-live-corpus.json`；停服务=结束 node 进程。
- **所需权限**：本机文件写入 + 启动本地服务（宿主机，非共享环境）。

## R5 扩展（RAG 审计 run 关联，随 R5 镜像层一并评估）

rag-mcp-server.mjs 的审计记录目前无 run_id（仅 query_hash+ts），run 关联靠 manifest 时间窗推断。将 run_id/trace_id 注入审计记录需改 worker 侧 MCP server（镜像层）——并入 R5 的镜像改动评估，不单独立项。

## 已知挂起（引用，非新请求）

- **密钥轮换**：用户决定挂起（DECISIONS #5）；M4 出口条件强制恢复执行。

## v3 真实验证范围（并入 R1/R2，架构 v3 落地后新增）

架构 v3（ARCHITECTURE-V3.md）本地骨架已完成；真实 Agent 验证是其第 11 步，并入 R1/R2 的真实案例轮：
- 同一 PR 两审查器真实并行（非本地线程模拟），验证 worker 占用与消息隔离；
- 风险分级真实路由（Trivial/Lite 各一例 + 敏感路径一例）与 run-manifest 中的规则落盘；
- 聚合去重与 verifier 独立性的真实证据（result.md 引用来源分离）；
- 关键审查器超时的真实降级（可选：注入单 agent 超时，观察 REVIEW_PARTIAL 发布）。
每项独立触发条件与判据，**不预设一轮完成全部验收**。

## 建议执行顺序

R4 备份先行 → R5 只读探查（零授权成本，可与任何项并行）→ R1+R2+R4 回归合并为一轮真实案例 → R3 附属于该轮 → R6 在 R5 方案明确后择路。
