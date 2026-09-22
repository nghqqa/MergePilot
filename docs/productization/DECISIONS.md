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
