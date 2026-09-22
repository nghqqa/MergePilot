# 产品化关键决策（DECISIONS）

## #1 M1-0 调用链与状态归属（2026-09-22，调研结论）

- **编排事实源现状**：`github_deliveries.status`（服务器 PG）是投递队列事实源；MinIO 项目 meta.json 是单次审查运行的权威进度/终态；GitHub check-run + MinIO receipt（本批新增）是发布事实。**结论：交付生命周期 = 台账状态 × 项目状态 × 发布凭据三方会合，桥是唯一的会合执行者**（Controller 未运行）。
- Controller `github_drain` 具备更完整的合同（CTE+SKIP LOCKED、过期 RUNNING 接管、claim 轮换、stage_events 幂等、attempts=5），但依赖 P14 的 submit_task/Gateway，与 AgentTeams 控制面冲突（v2 备忘已知）。**M1 策略：桥先对齐其合同语义（精确 claim/有界/对账），行为对照点留作 Controller 接管时回归基准**——非把 Controller 拉进现网。
- receiver 语义复核：INSERT-only、delivery GUID 为 PK（同 GUID 重发不产生新行）、HMAC 先于一切 DB 写。

## #2 M1-1 发布语义加固（2026-09-22，已测试）

- `post_check` 拆为 payload 构造 + `_reporter_exec` + 结构化解析：**仅 HTTP 200/201 且带 check_run_id 视为发布成功**；stderr/异常一律失败。
- 发布顺序固定：**receipt → reconcile(GET check-runs 采纳同名 run) → POST(3 次有界+退避) → receipt 落盘**；receipt 写失败非致命（恢复由对账兜底）。
- `claim_id` 每次轮换 + `finish` 精确匹配（旧执行者 rowcount=0）。
- 去重守卫 `already_processed`：同 repo+PR+head 已 PROCESSED 则直接标记，不重跑。
- timeout 语义收紧：审查未终态时**即使 neutral check 已发布，投递也标 ERROR(TIMEOUT manual)**——防"发布成功掩盖审查未完成"。
- 桥 import 改为 repo 相对优先（tools/r3ops），绝对路径仅作回退。

## #3 副本事实源与部署（2026-09-22）

repo `tools/gh-bridge/gh_bridge.py` 为事实源；**运行副本在 `D:\goai\r3work\scripts\`（startup_fullchain 启动的是它）**。下次真实案例轮前需同步（copy 回 r3work）——列入 M4 前集成检查单，现在不同步（避免半更新的运行副本）。矩阵库依赖：repo 副本经 tools/r3ops 解析，r3work 副本两路均可。

## #4 测试路径修复（2026-09-22，顺手但必要）

仓库整理（决赛期 Dockerfile 移入 docker/）遗留 8 个 gh_app 测试失败/收集错误（引用根目录 `Dockerfile.*`）。已全部改为 `docker/` 路径——不修则 M1 无回归基线可跑。属路径对齐，非断言放宽。

## #5 密钥轮换挂起（用户决定，2026-09-21）

为测试方便暂不轮换 WEBHOOK_SECRET/reader/PUB_PASS；恢复时按 secrets-locations 备忘的四级联执行。**V0 内测（M4）前必须恢复此决策的执行**——已列入 M4 出口条件。

## #6 M2 审批语义重定义（2026-09-22，规格+纯逻辑层已实现）

- **票据机制形状复用 l2（存在性+状态流转+attempt），流程全部重定义**：V0 动作集={generate_patch, run_poc, publish_result}（启用子集=决策项 D-1）；**merge/close/revert 剥离不迁移**（v2 二.2a）。旧 `USED→MERGED/l2_done` 合并审批链留在决赛 demo 资产里，不改动、不导入。
- **绑定五元组**：run_id + repo + head_sha(40hex) + params_hash(64hex) + 补丁/finding 指纹，frozen 不可变；执行前逐字段校验（红线"批A执B"的防线），拒绝先于副作用。
- **PR 更新失效双保险**：显式 INVALIDATED 标记（门页展示用）+ 正确性独立于标记（新 run/head 必然绑定不匹配）。
- **竞争仲裁**：PENDING 唯一分支态，approve/reject CAS 先到先得，幂等重放 NOOP；同 (run,action,finding) 活动票幂等返回；终态后允许 attempt+1。
- **审批期限语义**：approve/start_exec 过期即拒；已开始的执行允许收尾（执行时长归编排侧 deadline，不重复设门）。
- **决策项未拍板**（规格 §6）：D-1 启用动作子集、D-2 审批人权限映射、D-3 TTL 默认值。拍板前校验器只用于隔离测试（测试身份 test-approver），**不接真实执行路径**。
- 实现形态：tools/approval/ 纯逻辑零依赖；生产存储在门 Web 化工作项接入（PG approvals 改造 vs MinIO 票据对象，待预研）。

## #7 记录勘误：gh_app 测试计数（2026-09-22 第二轮实测）

干净树 8b30fb1 实测 gh_app = 821 collected（816 passed + 5 skipped），与 #4 时期记录的"831 passed"差 15。tests/gh_app 自 b46e8ba 字节未变 ⇒ 差额不可能来自提交内容，判定为当时未跟踪文件混入或转抄误差。**以 821/816 为准**（已订正 STATUS/ACCEPTANCE）。无失败用例，M1 回归结论不变。

## #8 互斥边界客观核实：lease_expires_at（2026-09-22）

桥 claim 不写 `lease_expires_at`（NULL）⇒ github_drain 接管谓词（`lease_expires_at < now()`）对桥在途行**恒不成立**——Controller 抢不走桥的行，这是结构保证而非约定。反向由 `%-bridge-%` 命名空间保证（UUID 字符集不含 b/r/i/g 不会撞段）。**代价**：cutover 时桥在途行对 Controller 不可见，必须按契约 §6 三步程序（桥停认领→排空→启 drain），否则产生只有桥能回收的孤儿行。已写入 ORCHESTRATION-CONTRACT.md §6。

## #9 run-manifest missing 项来源（R5 只读探查结论，2026-09-22）

| 项 | 来源（只读） | 状态 |
|---|---|---|
| 模型标识 | worker `/root/.copaw-worker/{role}/openclaw.json` → `agents.defaults.model.primary`（reviewer 实测 = `agentteams-gateway/deepseek-chat`）。**只提取该字段**，不搬运其余配置 | ✅ 已接入清单 |
| Skill 内容哈希 | worker 镜像 `/opt/mergepilot/skills/{diff_parse,risk_classify,sast_scan,case_retrieval}`，sha256 of 排序逐文件 sha256；工具名→目录名映射 `_SKILL_DIRS`（`skill_*`→无前缀，**映射错会静默产出空串哈希**——已修并加空目录守卫） | ✅ 已接入清单 |
| Worker 镜像 ID | `docker inspect --format {{.Image}} elemiso-worker-{role}`（容器名带 `-worker-` 段） | ✅ 已接入清单 |
| RAG 版本 | 配置只有 `:4184` endpoint（rag-live 未运行），无版本可取 | ⬜ 保持 missing |
| 生成参数 | agentloop 不外露 | ⬜ 保持 missing（不伪造） |

冒烟实测（只读，2026-09-22）：清单 missing[] 只剩上两行。教训：探查脚本对"哈希了空输入"必须显式失败——e3b0c442…（空 sha256）曾冒充四个 skill 的哈希，被只读冒烟抓出。

## P-1【已决定，工程自主】门票据存储 = SQLite WAL（2026-09-22 第二轮，推翻原 MinIO 提案）

原提案（MinIO 单写者）**否决**：mc 客户端无条件写原语，"单写者约定"无法证明并发正确性，而门页多请求并发批准/拒绝是真实场景。**决定**：`tools/approval/store_sqlite.py`（SQLite WAL）——真跨进程 CAS（BEGIN IMMEDIATE 写锁排队 + 前置状态守卫 UPDATE rowcount）、活动票唯一由 partial UNIQUE INDEX 跨进程强制、WAL+synchronous=FULL 崩溃恢复、零新服务、单文件可备份。**状态机不重写**：存储层复用 approval.transition 纯逻辑（34 单测语义零漂移）。测试证明跨连接竞争（approve/reject 先到先得、并发创建收敛同一票）、崩溃重开续转移、红线校验往返成立。**迁移路径**：Controller cutover 需要服务器共享存储时按同形状迁 PG，语义单测不变。这是可逆的普通工程选择，不涉共享环境变更。

## #10 RAG 用途认定与修复范围（2026-09-22 第二轮）

- **认定**（依据语料 disclosure+审计记录 data_mode，无产品范围歧义，不提请决策）：A 知识链（rag_retrieve，组织安全标准，advisory）为 V0 RAG 验收主场景；B 案例链（skill_case_retrieval，历史案例，repo_scope 隔离）为补充，非零命中行为未验证；仓库代码检索不走 RAG（reviewer 直接 clone）。
- **修复**：语料事实源入库 + corpus_tool（内容寻址 snapshot_id、幂等导入）+ 服务事实源入库；桥派发前绑定快照 + RAG_REQUIRED fail-closed 门（默认 advisory 保持现语义，降级经 manifest 可见）。运行副本（r3work）零改动。
- **遗留待授权**：审计 JSONL 加 run_id（运行时变更）；第 3/4 层验证；语料部署同步（R7）。

## #11 预算重试语义复查结论（2026-09-22 第二轮）

`reserve(retry_of)` 是**预留去重**（同一逻辑调用不重复占额），不是成本豁免：真实重发的模型调用照常计费，结算必须传累计实际用量（test_retry_reissued_call_commits_cumulative_cost 固化）。脚手架仍未接真实调用路径——硬预算验收不变。
