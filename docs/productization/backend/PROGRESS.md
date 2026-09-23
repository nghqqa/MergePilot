# 后端执行进度（BACKEND PROGRESS）

**分支**：`feat/backend-pg-storage`（基于 `chore/backfill-r3-ops` @ `67d6446`）｜ **负责窗口**：后端实现
**设计基线**：`docs/architecture-audit-20260922` @ **`caf6909`**（用户已接受 c664df2 + 210f70c；机制增补 f588302 已采纳）
**文件责任边界**：本窗口拥有 `tools/approval/store*`、`tools/integration_prep/`、`tools/costmeter/hooks.py`、`tools/console_pg/`、`tests/approval/`、`tests/integration_prep/`、`tests/console_pg/`、`docs/productization/backend/`；**不修改** ARCHITECTURE-V3（设计窗口）与 tools/console_v3（管理平台窗口）。

## 基线与状态

- 基线提交：`c179601` → 后续：`51e1a89`（执行保护+迁移 runner）→ `4a686a8`（对账身份+RunStore+前端交接）→ `0500a12`（PGTicketStore）→ 当前
- 设计基线：caf6909（target_key/NULLS NOT DISTINCT 两形态均合规，0500a12 实现已确认合规）
- 机制增补：f588302 P1 finding anchor 校验（后续工作包）、P2 有界取消（设计契约已读）、P3 反馈/版本化
- 认证/合并设计：7ccecb9 auth v2（会话/Capabilities/CSRF——后端暂不实现，留待 D-9）

## 已完成（本轮）

| 工作包 | 内容 | 验证 |
|---|---|---|
| 执行保护① 定向认领补 repo | 过滤三元组 = repo+PR+完整 head（`MERGEPILOT_TARGET_REPO/PR/HEAD`，三全才激活，格式非法拒绝） | 回归测试更新+新增（缺一即不激活/非法拒绝/SQL 谓词断言） |
| 执行保护② 发布五分类 | reporter 脚本结构化 HTTP 错误；outcome ∈ {unknown, auth(401), retryable(429/5xx/403限频), permanent(403非限频/404/422)}；unknown → PUBLISH_UNKNOWN(manual-reconcile) 停止自动重试 | 7 项分类测试 |
| 执行保护③ 取消范围 | 纯逻辑计划（先 leader 防再委托 → reviewer/fixer/verifier → 静默核验 → 在途计费提示 → 恢复注记）+ 前置校验（独占+无其他活动项目）+ 取消后核验 | 5 项纯逻辑测试（容器操作本轮未执行） |
| 隔离 PG 环境 | 容器 `mp-pg-contract-test`（postgres:16，127.0.0.1:55432，db=mp_contract，独立卷）；psycopg2 2.9.12 可达 | 连接+版本实测 |
| PG 可行性 spike | 部分唯一索引（活动票唯一）+ 前置守卫 UPDATE CAS + 终态让位新 attempt——在真实 PG 16 上 3/3 通过 | tests/approval/test_pg_cas_spike.py |
| 契约测试抽取 | tests/approval/store_contract.py：7 项契约（创建往返/幂等创建/CAS/跨连接竞争/并发收敛/崩溃恢复/红线往返）——实现无关，PG 适配器落地后直接复用 | SQLite 上 50 passed |
| 迁移源盘点工具 | migration_inventory.py（PRAGMA 只读盘点表/列/DDL/行数 + 兼容性注记） | 双库演练：tickets(18列)/v3_runs(22列)；注记=expires_at/approved_at 无类型声明列迁 timestamptz 需格式校验 |

## 版本一致性（repo vs r3work 运行副本，2026-09-22 实测）

- `gh_bridge.py`：**不同步**——repo 侧含本轮执行保护修复（repo `e68db380…` vs 运行副本 `840b193c…`，R4 时的版本）。
- orchestrator 10 文件：全部一致。
- 结论：真实案例（待授权）前需要一次**增量同步**（仅 gh_bridge.py），方案同 R4（备份→校验→复制→off 冒烟），**待新增授权执行**。
- 明细：backend/version-delta-20260922.json

## 已完成（本轮二：PostgreSQLTicketStore 实现+真实隔离 PG 验收）

设计基线：`docs/architecture-audit-20260922` 分支 e247b80 的 DATA-ARCHITECTURE-PG.md §5.4（列形状/状态枚举/部分唯一索引/ticket_audit 同事务均已明确，作为实现基线；版本已记录）。

| 工作包 | 内容 | 验证 |
|---|---|---|
| 迁移文件 | tools/approval/pg/migrations/001_approval_tickets.sql（schema approval + tickets + ticket_audit + 夹具父表 run.repos/run.runs[仅满足 FK，正式定义属设计文档]） | 在隔离 PG 应用通过 |
| PostgreSQLTicketStore | tools/approval/pg_store.py：实现 TicketStore 接口；复用 approval.transition 纯逻辑（零状态机重写）；参数化 SQL；显式事务+回滚；SELECT FOR UPDATE + 前置守卫 UPDATE（CAS 可判定）；ticket_audit 同事务；StorageUnavailable 与业务拒绝分离；时间统一 aware UTC（ISO 自动归一）；store.py 再导出（默认路径不切换） | 真实隔离 PG 契约集 7/7 + PG 专属 9/9 |
| PG 专属验收 | NULL(finding_id) 唯一性[NULLS NOT DISTINCT]/非 NULL 唯一/不同目标不互斥/**跨进程竞争（spawn 独立进程）恰好一胜**/终态让位新 attempt/时区与到期边界/回滚原子性（审计与状态同事务）/最小权限（runtime 角色可 DML 不可 DDL）/审计追加核对 | test_store_pg.py 16/16 |
| 迁移工具+演练 | migrate_tickets_sqlite_pg.py：源只读/默认 dry-run/校验（状态/形状/时间/重复活动票）/父记录缺失跳过不伪造/冲突不覆盖可重跑/逐字段核对/事务失败整体回滚 | 测试副本→隔离 PG 演练 5/5（dry-run 不写/导入核对/重跑幂等/非法源中止/孤儿不伪造） |

## 已知偏离与实现期修复（v1 阶段记录，历史保留）

1. ~~NULLS NOT DISTINCT~~：已被 target_key 方案取代（见本轮三）。
2. **实现期真实缺陷修复**（非测试放宽）：
   - `create()` 未把 approval_expires_at 传入票据（到期永为 NULL ⇒ 永不过期）——已修；
   - PG TIMESTAMPTZ 返回 datetime 与调用方 ISO 字符串 now 不可比——transition 入口归一化；
   - `_expired` 源头容错 ISO 字符串/naive UTC（纯逻辑小改，语义不变，SQLite/PG 共同受益）。

## 已完成（本轮三：设计基线 caf6909 对齐 + PG RunStore 最小纵向）

**设计基线已固定并记录：`docs/architecture-audit-20260922` @ `caf6909`**（含 ad4ce90、374851b 累计修订；旧基线 e247b80 的 NULLS NOT DISTINCT 偏离已被 target_key 方案**取代并废弃**——非"待确认"状态）。

| 工作包 | 内容 | 验证 |
|---|---|---|
| 审批 target_key 对齐 | 002 迁移（加列→回填 COALESCE(finding_id,'_run_')→NOT NULL→换索引 uq_active_ticket）；pg_store 派生 `target_key_for(binding)`（内部派生，外部不可注入） | 真实 PG 16/16 |
| target_key 专项 | run 级='_run_'/finding 级=finding_id/相同目标不重复活动票/不同目标不错误互斥/外部伪造 target_key 不能绕过绑定校验（仍 BINDING_MISMATCH）/空串 finding_id 在绑定校验即拒绝（不静默转换） | 5 项 |
| 发布身份修复 | `decide_reconcile_adopt(matches, recorded)` 纯函数：recorded 优先（权威凭据）、单 match 采纳（app 归属校验）、多条/record 不符→歧义人工；reconcile 失败→UNKNOWN 禁止盲目 POST；循环后兜底对账；**旧"同 repo+pr+head ⇒ 同 run"假设已随 run 身份 v2 废弃** | 桥发布测试重写+新增（gh_bridge 67 passed） |
| PG RunStore 最小纵向 | tools/orchestrator/pg_runstore.py + 003_run_domain.sql（run.repos/targets/runs/stages/run_events；findings/validations/attempts 留待后续包；delivery_id/first_delivery_id/knowledge_manifest_id 无 FK——共享表不在隔离实例，已登记偏离）：确定性 run_id（§3.2 规范 JSON+向量锁定测试）/request_key 幂等重放/exec_seq target 行锁分配/活跃部分唯一/INSERT-only/supersede 链接不改写历史/阶段+事件同事务/期望状态守卫 | 真实 PG 10/10（含 spawn 双进程并发首建收敛同一 run、恰好一创建者） |

## 已知偏离与实现期修复（记录，不属设计窗口拍板范围外自作主张）

1. **NULLS NOT DISTINCT（历史项，已被 target_key 取代）**：v1 索引的 finding_id=NULL 漏洞由设计 v2 的非空 target_key 修复；002 迁移为显式后续（不回改 001）。
2. **实现期真实缺陷修复**（非测试放宽）：
   - `create()` 未把 approval_expires_at 传入票据（到期永为 NULL ⇒ 永不过期）——已修；
   - PG TIMESTAMPTZ 返回 datetime 与调用方 ISO 字符串 now 不可比——transition 入口归一化；
   - `_expired` 源头容错 ISO 字符串/naive UTC（纯逻辑小改，语义不变，SQLite/PG 共同受益）；
   - gh_bridge 曾出现 parse/decide 函数重复定义（区间重写事故）——已整体重建为单一定义（382–429 行区）。

## 待设计窗口决定（剩余接口问题）

- knowledge_manifests 与 run 的关联（runs.knowledge_manifest_id 目标表已定义，未落迁移）；
- findings/validations/stage_attempts 迁移（设计已给形状，最小闭环未含）；
- v3_hook_errors 的统一归属（现 SQLite）。

## RunStore 后续（已实现最小纵向，扩展项）

后续包：stage_attempts 记录、findings/validations 落库、delivery 1:N 关联的共享库 FK 补齐（统一迁移时）。

## 待外部输入（真实案例线，集中列出）

1. 外部写入与服务变更范围：空提交触发 + check-run 发布 + rag-live/worker 容器操作（CASE1-RUNBOOK §7.1/7.3）；
2. 预算金额与有效限制方式：provider 侧 gateway 上游 key 消费硬上限（本地网关 key 非计费凭证；限频/超时/事后计量均非硬预算）；
3. 凭证处理决定：现网凭证（未轮换）是否满足本次真实运行条件。


## CASE1 真实 PR 审查结果（2026-09-23）

**首次真实 PR 审查完成**：PR #9（feat/skill-exercise）head `89c65a47` 经加固旧链路（leader+reviewer 真实模型调用，agentteams-gateway/deepseek-chat）完成审查。

| 维度 | 结果 | 证据 |
|---|---|---|
| 真实 PR 审查 | ✅ 完成 | 加固桥首次真实案例通过 |
| GitHub check | ✅ 已发布 | `mergepilot/review` check_run `107056305844` on head `89c65a47`，conclusion=success |
| 台账 | ✅ PROCESSED | `completed/pass; check_run=107056305844` |
| 运行副本 | ✅ 已同步 | 12 文件 sha256 一致（R4 授权，备份 .bak-20260923-050213） |
| RAG | snapshot 已绑定 | `fd34c304` 写入 manifest；reviewer 未调用 rag_retrieve（PASS 无 HIGH finding），rag-live 已按 R7 停止 |
| 凭证 | 现网可用 | 用户确认（D-6），未轮换 |
| 预算 | 人工兜底 | 无 provider 硬上限；20min 截止+人工停止（D-4） |

**口径**：这是**加固旧链路的首个真实案例**。不等于 M1 通过（其他故障场景未验证），不等于 M2 真实审批启用（D-1/D-2 未拍板），不等于 M3 隔离验证，不等于 M4 V0 就绪。证据包见 `.case1_evidence/`（不入库）。

## 里程碑状态（CASE1 后）

| 里程碑 | 状态 | 说明 |
|---|---|---|
| M1 可靠性接管 | 部分 | 加固桥首次真实案例通过；其余故障场景待 R1 授权 |
| M2 审批正确性 | 部分 | 机制完备+隔离验证；真实审批待 D-1/D-2 拍板 |
| M3 隔离证明 | 未开始 | 需真实 run 后污染测试 |
| M4 V0 内测 | 未开始 | M1–M3 + D-1/D-2 + 预算前置 |
| 真实 PR 首例 | ✅ 本轮完成 | 见上表 |


## 第二十六轮（2026-09-23）：CASE1 证据复核 + run 上下文透传 + 模型切换准备

### CASE1 证据审计结论（标注:**旧链路首个真实正常案例**）
- 台账/GitHub/manifest/审计/回执五方一致:delivery `90cf0420` PROCESSED、check-run `107056305844`(app=mergepilot-reporter,head/conclusion 匹配)、receipt(MinIO,adopted=false)、manifest bridge_source_sha256=bb1fafae 与 repo 桥逐字节一致。
- 审计流(143 条)中 CASE1 窗口仅 skill_diff_parse+skill_sast_scan 两条,**无 rag.retrieve 调用**(PASS 无 HIGH,合理);rag-live 当时在 :4184,manifest `service_state_at_dispatch=unreachable` 系桥探测默认端点差异。
- **无崩溃/重试痕迹**:投递行单次认领(05:21:52)无 RQ/ERROR;项目目录仅一套 manifest;09-23 当日审计流无更早 reviewer 调用。result.md 的 "(re-run)" 系 leader 自身叙述(大概率受 09-19 同 PR 两个历史项目影响),不构成编排事实。
- 口径不变:不标 M1 完成,不标真实 RAG 已验证。

### run_id 透传修复(R5)
- 新 `tools/gh-bridge/run_context.py`:可信 run 上下文(run_id/attempt_no/repo/PR/head/skill_digest/retrieval_mode/manifest_id),字段只来自 run-manifest(write-once)+投递行+桥自身计数;`authored_by=gh_bridge` 契约,模型输出/请求参数不可写。
- 桥接线:派发边界写 MinIO `run-context.json`(write-once,冲突 fail-closed)+审计流 `bridge.run_context`/`bridge.run_end` 边界记录(best-effort advisory);终态补右边界。不改既有执行逻辑。
- 隔离回放:`attribute_audit_calls()` 窗口相关法(毫秒级时间戳边界处理;strict 模式无边界记录即拒判)。CASE1 真实数据回放:2 条归入、141 条如实不归属(r3work/model-switch/case1-replay-attribution.json)。

### 模型切换准备(DeepSeek Flash)
- 事实:链路 = worker → higress(elemiso-controller:8080/v1,OpenAI-compatible)→ api.deepseek.com。**上游实时目录仅 deepseek-flash/deepseek-v4-pro**;deepseek-flash 为上游真名非本地 alias。deepseek-chat 已不在目录但**实际调用仍 200**(退役不停服,回滚路径仍可用)。
- 配置面 `tools/model_gateway/`:MERGEPILOT_MODEL/PROVIDER_BASE_URL/MODEL_API_KEY_ENV(只存变量名)/TIMEOUT_S/MAX_ATTEMPTS/TEMPERATURE/MAX_TOKENS;默认值=已验证 deepseek-chat。切换操作杆=MinIO `agents/<role>/.copaw.secret/providers/active_model.json`(+openclaw.json 同步)+worker wake。
- key 坑:provider json 的 api_key 为 ENC: 加密态(直打网关 401);生效明文键在 openclaw.json `models.providers["agentteams-gateway"].apiKey`。
- 隔离 smoke 八点 **PASS**(reviewer 容器内、生产同路径):目录含 deepseek-flash;最小请求 200 响应 model=deepseek-flash(无静默改写);usage 完整(含 prompt_cache_hit_tokens/reasoning_tokens);401→auth/400→permanent/timeout→timeout 分类正确;429/5xx 分类由单测覆盖不在线制造;零 GitHub 写。报告:r3work/model-switch/smoke-report-20260923.json。
- manifest 增 `model.requested`+`model.catalog_state_at_dispatch`(派发时网关实时目录,advisory;未探明进 missing[])。

### 测试
- 新 29+5 用例:run_context 契约/回放、model_gateway 配置/分类/smoke 离线驱动、桥接线 fail-closed;既有 96 桥用例全绿(桩对齐三元组)。


## 第二十七轮（2026-09-23）：deepseek-flash 官方路径切换生效 + 第二案例验收就绪

### 模型切换（已生效，可逆）
- **平台原生操作杆 = `agt update worker --name <role> --model deepseek-flash`**
  （controller 更新 Worker CR spec.model → 再生 openclaw.json 推 MinIO → sleep/wake 重建容器）。
  手工改 MinIO/容器文件都会被 reconcile 翻掉（merge-openclaw-config local-first + worker 启动
  mirror_all remote→local --overwrite + controller 按自身模板重推）——此坑已实测记录。
- 切换后验证：reviewer/leader 两容器 openclaw primary = agentteams-gateway/deepseek-flash、
  active_model.json = deepseek-flash、models map 含 flash；桥探针 `_worker_model_id` 读回 flash；
  manifest 模型块 = {primary: flash, requested: 派发时 env, catalog_state_at_dispatch: 实测目录}。
- 门禁顺序执行：配置摘要（无秘密）→ MinIO+本地双备份(.bak-20260923-151612) → 上游目录核对 →
  reviewer 容器 smoke 八点 PASS(切换前+切换后各一轮) → 回滚探测(deepseek-chat 仍 200) → 零 GitHub 写。
- 回滚 = `agt update worker --model deepseek-chat`（上游退役但仍服）+ 备份对象。
- 默认配置未动：代码/桥默认仍 deepseek-chat；MERGEPILOT_MODEL 未设。

### 第二案例（未执行——目标/空提交未获明确授权）
- 零 GitHub 写入；仅交付验收采集就绪清单：run_context 八字段全自动入审计流
  （本桥版本起）；rag-live 审计含 result_status/document_count/latency；manifest 含
  rag 快照+model 目录双绑定。差距登记：①rag_retrieve call_id 不入审计（仅窗口相关回放）；
  ②RAG 审计仍在 :4184 JSONL，PG audit 域(P3)未实现；③响应侧 model id 容器内不回传。
  授权后的执行清单见用户 §三/§五（单 repo/PR/head、无 fixer/verifier、v3 shadow、
  单 check-run、20min 截止、失败处理矩阵）。

### 测试口径（TEST-DEBT.md 建立）
- 收集冲突已修：pyproject `addopts=--import-mode=importlib`，全树 4345 项/0 收集错误。
  （补 __init__.py 方案会破坏 conftest sys.path 自举，已实测回退，勿重试。）
- 全树实测基线：84 failed + 21 setup errors，全部集中于环境/产物依赖套件
  （m5_0c resolver 需 docker CLI、demo_console 缺烘焙产物、isolated_live 需 compose、
  m4c/m4e/m4f1 证据线）；清单+收口方向登记在 docs/productization/backend/TEST-DEBT.md。
  本轮门禁维持相关目录 101+ 全绿。


## 第三十轮（2026-09-24）：CASE2 真实 RAG 消费达成（A1-A5 获批执行）

**授权范围**：nghqqa/fastapi-boilerplate-demo PR #2；A1 空提交 / A2 flash 真实审查 / A3 rag-live 启停 / A4 单 check-run / A5 费用风险接受。fixer/verifier/故障注入/自动修复/merge 未授权未执行。

### 执行时间线（全部真实调用，无 fixture/shadow）
1. 门禁七项全绿（flash 生效、副本同步、认领 fail-closed、语料在位、台账零 PENDING）。
2. A1：单一空提交 `65de83d..254f61c`，新 head `254f61ce2ff5`，远端 tip 核实一致。
3. webhook 入账 `82c9c210` PENDING → 定向认领唯一 run `run-gh-pr2-254f61ce-104621`。
4. rag-live :4184 启动（health OK，12 chunks SYNTHETIC）。
5. **首次尝试 kickoff 失败**：matrix.py 读已删除的决赛期密钥文件路径（ctrl.env 已清理）。
   无业务副作用（kickoff 未发、零模型调用）。处置=环境变量注入同一凭据（零代码改动）+
   桥自身 requeue 语义 CAS 恢复投递行 + 清理本案例失败尝试的 4 个 MinIO 工件（已归档
   r3work/model-switch/case2-failed-attempt/）。**运行时代码未改、未同步共享副本。**
6. A2 执行：审查员 skill_diff_parse + skill_sast_scan（req-32fada7f83/req-8fa470c5fb）→
   **rag_retrieve 自然触发 2 次（docs=3，命中 cwe-22-path-traversal#1 + file-path-containment#1）**
   → 结论 **FINDING_CONFIRMED / HIGH / HUMAN_VERIFICATION_REQUIRED=YES**（CWE-22 任意文件读，
   独立 PoC：base 目录外文件 HTTP 200 读出、/etc/hostname 可读）→ leader 按规范停人工门。
7. 20min 截止 → conclude(timeout) → **唯一 check-run `107153647233`**（mergepilot/review，
   app=mergepilot-reporter，head=254f61ce，conclusion=neutral=超时语义）→ receipt 落盘
   adopted=false → delivery=ERROR `TIMEOUT(manual); publish=ok`。
8. **run_context 首次全链生效**：bridge.run_context/run_end 边界记录入审计流（run_id+
   attempt_no=1+manifest_id dd8b2acd…）；回放归属窗口内 7 条记录。

### RAG 消费判定（首个真实消费案例）
审查员 findings 明确引用 rag_retrieve 返回的 source_refs（cwe-22 定义+文件路径包含性组织标准）
作为定级佐证——**自然触发、有引用、无强迫**。BM25=lexical-zh-en-v1。skill_case_retrieval 返回
CASE_RETR_SCOPE_MISSING（如实记录）。sast_scan 0 findings（语义型漏洞非 AST 模式）已被审查员
独立复现推翻，未影响结论。

### 已知缺陷（如实登记，待修）
- manifest `rag.service_state_at_dispatch=unreachable` 与事实不符：桥从宿主机探测
  `host.docker.internal:4184`（该名宿主侧解析失败）；实际服务可达（审计佐证）。修法=宿主侧
  探测改 127.0.0.1（advisory 字段，未影响执行，未当场改代码）。
- 主循环异常路径 finish() 未带 claim 匹配导致首试行残留 RUNNING（本轮以镜像 requeue 恢复）。
- leader 停人工门时项目永不终态→只能以 timeout/neutral 收尾；action_required 结论在现行桥
  语义下不会出现（post_check 有映射但 conclude 不产出）——人工门语义待后续轮设计。

### 状态
- 人工门**等待操作员决策**（HIGH + HUMAN_VERIFICATION_REQUIRED=YES；未批准、未修复、未 merge）。
- rag-live 已停；worker 容器维持执行前 Up 状态；无未终止的在途请求（终态后无新模型调用）。
- M1 部分（+1 真实正常案例，gate-wait 路径）；M2 部分；M3 未开始；M4 未开始。
- **首个真实 RAG 消费达成 ≠ RAG 有效性证明**（单案例）。


## 第三十一轮（2026-09-24）：CASE2 暴露问题收口（三项修复+凭证预检）

基线 b6e338f。本轮授权=本地修复/隔离测试/脱敏分析/文档/本地提交；**未同步运行副本**（§三约束，待用户确认后一次性同步）。

### 事实校准（不改历史证据）
- CASE2 MinIO 工件/台账/归档副本逐字节未动（哈希在案）。
- **票据核实**：桥从未接 TicketStore——CASE2 **无正式审批票据**（结构性事实）；gate 证据现状=审查员/leader 模型文本+无 human-gate 文件+无票据，三者都不构成可信审批。
- 身份三层现状：①**边界关联=已实现**（bridge.run_context/run_end + 窗口回放）；②**逐调用身份透传=未实现**（skill/RAG MCP 调用不带 run 身份，只有窗口归属）；③**PG 审计落库=未实现**（审计仍 :4184 JSONL，PG audit 域 P3 未动）。
- “终态后无新调用”≠“无在途请求”：**无请求级证据，无法确认**（如实声明）。

### 修复1：RAG 探测分离+失败分类
宿主桥探测默认 `127.0.0.1:4184`（回环），容器侧检索地址（host.docker.internal:4184）为独立网络面由镜像持有，不做全局替换。`_rag_service_state()` 返回 {state, endpoint, failure_kind(dns/connect/timeout/http_N), detail}——**DNS/连接失败不再表述为服务已停**。manifest 增 `rag.service_probe`（端点/失败类型/时间/注记），`service_state_at_dispatch` 取 reachable|probe_failed。required 门仅 reachable 放行（probe_failed=fail-closed）。

### 修复2：人工门结构化处理（先核契约再实现）
- 契约核对：post_check 本就有 verdict→action_required 映射（"gate"）但 conclude 从不产出；gate_record 只读 approval/rejection 文本文件（决赛遗留，弱归属）。
- 新契约：leader 停门前写 `projects/<proj>/human-gate-required.json`（kickoff 明确 schema：version/run_id/task_id/severity/requested_by=leader/requested_at）；桥 `gate_marker()` 校验**结构+归属**（run_id/task_id 精确匹配本执行，非 leader 作者/坏 severity/坏 version 一律拒绝并记日志）——**模型自然语言不构成机器可执行 gate 证据**。
- watch_run：终态优先于标记；合法标记→提前返回 "gate"；无标记无终态→真超时。conclude：gate→verdict="gate"→发布 action_required（沿用 reconcile-first/单次发布/回执约束）→ delivery=ERROR `GATE_WAIT(manual)`（业务未终结，批准/拒绝属后续决策）。三分支严格区分：gate≠timeout≠普通完成。
- 信任层级如实声明：标记与既有 approval 文件同级（项目目录命名空间+leader 角色归属），**不承担跨身份认证**——该升级属审批票据域（TicketStore），列为待办。

### 修复3：CASE_RETR_SCOPE_MISSING
追踪结论：scope 来自部署侧 env（MERGEPILOT_CR_REPO_SCOPE/PG_DSN），reviewer 容器初始 env 两项皆无；干净复现=CASE_RETR_DB_UNAVAILABLE（dsn 先失败），审查员会话 env 有 DSN 无 scope 故报 SCOPE_MISSING。**scope 值可由桥的可信 run-context 确定**→repo 侧实现 `MERGEPILOT_CR_REPO_SCOPE_FILE` 透传（只认 authored_by=gh_bridge 的 run-context.json，取 code.repo；形状/作者不符→保持 SCOPE_MISSING 明确失败，不伪造不扩大）。**部署通道（容器 env 注入或 shared 路径传递）未打通**——需共享环境授权，列为最小决策项。

### §三：凭证预检前移
matrix.py 新增 `preflight()`（复用既有加载：env 优先/secrets 文件回退；脱敏诊断不含凭据内容）。桥 main() 在 target 校验后、**认领与任何 write-once 工件创建之前**调用，失败 exit(2)——杜绝 CASE2 首试“已认领+已写工件才发现发不出”的残留。matrix.py 正典收编 repo tools/gh-bridge/（运行副本已还原冻结）；桥优先本目录 import，回退旧布局；**运行副本 matrix.py 缺 preflight 时桥拒绝启动**（fail-closed，逼出显式同步）。手工删 manifest/重置台账未变成机制（本轮未新增任何此类代码）。

### 验证（隔离+脱敏回放）
- 新增 tests/gh_bridge/test_case2_fixes.py 27 用例：探测分类（dns/connect/timeout/http）、默认回环端点、required 门 probe_failed 拒绝、gate/真超时/成功三分支、终态优先级、错误归属拒绝（run_id/task/作者/severity/version）、自然语言≠证据、无凭证不认领不建工件（exit 2）、scope 文件透传+外来作者/缺文件/超长 repo 拒绝、CASE2 脱敏审计回放（边界关联法+无 call_id 口径）。
- 回归：tests/gh_bridge+model_gateway **128/128 绿**（旧桩对齐新探测契约，断言收紧非放宽）；tests/skills **75/75 绿**（case_retrieval 改动零破坏）。
- 历史 CASE1/CASE2 证据哈希核对不变。全树债务清单（TEST-DEBT.md）无变化——本轮未触碰 84F+21E 所在套件。

### 运行副本状态（待确认后同步）
r3work/scripts/{gh_bridge.py,run_context.py}=上轮版本（CASE2 实测版）；matrix.py 已还原冻结。**下次真实 run 前必须一次性同步 repo→r3work**（含 preflight/gate/probe 三修复），否则新桥会因 matrix 缺 preflight 拒启（按设计）。同步属运行时变更，待批。


## 第三十二轮（2026-09-24）：人工门 TicketStore 闭环 + case_retrieval 部署接线准备

基线 9a998b3。授权=代码/测试/文档/本地提交；未 push、无真实审查、无付费模型、无 GitHub 写、未动共享 case-pg、**运行副本仍未同步**、controller env 仅本地准备未部署。

### 只读核对结论（不重复实现）
已存在并复用：approval.py 纯状态机（五元组 Binding/CAS/check_execution 红线）、TicketStore 协议（5 操作）、SQLiteTicketStore（WAL+BEGIN IMMEDIATE+partial UNIQUE INDEX 幂等）、PostgreSQLTicketStore（CAS+audit）、gate_cli 操作面、PG 迁移 001-002（tickets+ticket_audit）、存储契约测试架。
本轮新增：SQLite 库缺 audit 表（镜像 PG 001 补齐）、marker→ticket 创建、门状态映射、桥接线、部署接线准备。

### TicketStore 最小闭环（tools/approval/gate_ticket.py）
- **marker → pending ticket 幂等创建**：action=generate_patch（既有动作集）、run 级审批（finding_id=None）、params_hash=canonical_hash(marker 载荷)、finding_fingerprint=canonical_hash(task/severity/repo/head)——既有字段值派生，无新字段。同 run/action/finding 活动票唯一（存储层强制），重复 marker 收敛同一张票。
- **绑定**：run_id/repo/head_sha 来自投递行与 manifest（可信），severity/task 经哈希绑定；marker 归属二次校验（version/run_id/task_id/severity/requested_by），任一不符拒绝建票。
- **CAS 决策**：approve/reject 先到先得；重复决策 NOOP/INVALID_TRANSITION（不覆盖历史，审计留痕尝试者）；D-2 无身份 approve=IDENT_REQUIRED；D-3 TTL 默认 72h（env MERGEPILOT_APPROVAL_TTL_H），过期 approve→EXPIRED；reason 落票据 error 字段+审计 request_hash。
- **marker≠批准**：建票只是把 Agent 请求结构化登记；决策必须操作员经 CAS 显式做出。
- **可审计**：SQLite 补 ticket_audit（append-only，与状态写回同事务）；两存储语义对齐收紧为"**每次转移尝试都留痕**"（成功/NOOP/被拒含 IDENT_REQUIRED/INVALID_TRANSITION/EXPIRED）。
- **修复两存储同一潜在缺陷**：纯逻辑状态机内部连带转移（过期 approve 的 PENDING→EXPIRED）此前不持久化——改为状态变化即写（守卫仍锚定 prev，CAS 不变）。

### gate 状态映射（纯函数，对 run 终态只读）
PENDING→GATE_WAIT；APPROVED→APPROVED_PLAN_READY（生成 fix/verify **计划数据**，auto_dispatch=False，不派发 fixer）；REJECTED→BLOCKED；EXPIRED→CLOSED_EXPIRED。TicketStore 不修改已完成 run 的终态、不绕过 PG/CAS/fencing；reconcile-first 与每 run 一个 check-run 约束未动。

### 桥接线
conclude gate 分支：marker→建票（幂等）→ticket_id 进台账 note；**归属 fail-closed**：marker.run_id≠manifest.run_id 或 manifest 缺失→拒绝建票（降级纯标记语义，明确记日志）；store 不可用→降级不阻断门。决策接口仍为本地隔离（gate_cli/console），桥不自动决策。

### case_retrieval 部署接线（本地准备，未部署）
tools/case_retrieval/deploy/：README（变量/行为/网络/挂载要点）、agentteams-cr.env.example + docker-compose.cr.example.yml（占位符，只读挂载示意）、validate_env.py（容器内启动前校验：DSN 缺=2、scope 缺/不一致/文件不可信=3、文件读取失败=4；脱敏不打印值；scope 唯一可信来源=gh_bridge run-context，作者不符拒绝）。**未向运行中 r3work/controller 注入任何配置**。

### 测试与债务
- 新增 tests/approval/test_gate_ticket.py **26 用例**（幂等/绑定/CAS/重复/TTL/身份/错误 head/重启恢复/审计/映射/桥归属/env 校验器）全绿。
- 回归：approval+gh_bridge+model_gateway+skills **279 passed/29 skipped** 全绿（含既有 store 契约集）；PG 门控套件 **25/26**——唯一失败 test_cross_process_race_single_winner 为**基线既有**（stash 对照验证；Windows spawn 队列怪癖），已登记 TEST-DEBT.md。
- 历史证据未动；失败清单口径不变。

### 状态声明（防口径漂移）
TicketStore 闭环=**已实现（代码+单测）**；PG 版=已有实现+审计对齐，本轮回归通过，但**未接任何真实部署**；决策接口=本地隔离工具，**真实审批仍未启用**（D-1/D-2 未批）；controller env=本地准备**未部署**；运行副本**未同步**；D-7/OAuth/fixer-verifier**仍未启用**。**单测通过≠真实审批验收通过**。


## 第三十三轮（2026-09-24）：TicketStore/接线验证 + 运行副本同步 + 隔离 smoke

基线 59ccd1a。授权=验证与隔离 smoke+同步批准的后端文件；无真实审查/付费模型/空提交/check-run/真实 approve-reject/共享 case-pg 写入。

### TTL 口径统一（D-3）
发现不一致：AUTH-DECISION-PACKAGE D-3 建议案=**24h**（l2 惯例）、policy.py 既有默认=24，而上轮 gate_ticket/桥默认误写 72。**统一为 24h**（gate_ticket 默认参数+桥 env 默认），补三处一致性测试（test_ttl_default_is_24h）。历史轮 PROGRESS 中的 72h 为历史记录不改。

### 运行副本同步（含备份与回滚点）
- 同步前 hash 差异：gh_bridge/matrix DIFF，run_context 一致，approval 包 7 文件在 r3work **缺失**（首次落位）。
- 备份：`r3work/rollback-20260923-222605/`（scripts 三文件原版+ROLLBACK.md 清单；approval-old 空=原不存在）。
- 同步（repo→r3work）：scripts/{gh_bridge,matrix,run_context}.py + approval/{gate_ticket,approval,store_sqlite,store,policy,gate_cli,pg_store,__init__}.py → **11/11 hash 一致**。
- preflight 双向验证：旧 matrix.py（备份版，无 preflight）→ 桥拒启 exit=2（脱敏提示）；新 matrix+真实凭据 env 注入 → preflight ok。

### case_retrieval 隔离接线（isolated_smoke.py **9/9**）
隔离实例=mp-pg-contract-test（**非共享 case-pg**），一次性库 cr_smoke+migration 001（case_retrieval_reader 只读角色）。验证矩阵：①DSN 缺→DB_UNAVAILABLE ②scope 全缺→SCOPE_MISSING ③文件作者不符→SCOPE_MISSING ④文件缺失→SCOPE_MISSING ⑤有效配置→连接/只读角色/表能力校验通过，查询在缺 pgvector 处**干净失败不回退**（适配器 SQL 结构性以 WHERE repo_scope 起始=防全库回退）⑥-⑧validate_env ready(0)/no-dsn(2)/mismatch(3)。全流程脱敏。**缺口**：完整查询路径（pgvector 相似度检索）需含 pgvector 的隔离实例——登记待办。

### TicketStore 隔离 smoke（gate_ticket_smoke.py **13/13**）
a 幂等单票 / b 绑定字段 / c approve-reject 跨连接竞争唯一赢家 / d 重复决策不覆盖 / e TTL 内部转移持久化 EXPIRED / f 无身份 fail-closed / g 每次尝试留审计（3 条：被拒+成功+被拒） / h 桥只建票不决策（结构性断言）；端到端：marker→ticket→GATE_WAIT→approve(APPROVED_PLAN_READY 不派发)/reject(BLOCKED)/expire(CLOSED_EXPIRED)；映射纯函数对 run 终态只读。

### 回归与状态
- approval+bridge+gateway+skills **280 passed/29 skipped**；PG 门控 **25/26**（唯一失败=Windows spawn 竞争，基线既有，TEST-DEBT 已登记，口径未变）。
- **状态**：运行副本=已同步（本轮，回滚点在案）；case_retrieval=仅隔离实例接线，**未部署共享环境**；TicketStore=代码+隔离验证完成，**未接真实部署/真实审批**；approve/reject=仍是本地隔离接口；D-1/D-2/OAuth/fixer-verifier/真实 CASE2=**仍未启用**。隔离 smoke ≠ 真实审批验收。
