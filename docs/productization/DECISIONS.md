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
