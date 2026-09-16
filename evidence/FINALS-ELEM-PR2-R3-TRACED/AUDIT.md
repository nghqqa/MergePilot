# AUDIT — R3-TRACED 会话事故与偏差登记（PR2-R3-TRACED 与 PR3-R2-TRACED 共用，同栈连续运行）

> 原则：全部如实。宿主时钟 UTC。时间窗 2026-09-16 13:30Z–16:21Z。

## 1. 关键时间线

| 时间(Z) | 事件 |
|---|---|
| 13:30–14:19 | 环境侦查 + 埋点 v2 机制测试（基础镜像临时容器）。确认 v1 毒根 = .pth 期 `__import__` copaw 模块；同时发现第二毒根：taskflow/message 工具函数在 hooks 注册时被捕获引用，模块级 patch 对已注册工具无效 → v2 改用运行时按名查找的稠点表 |
| 14:26–14:51 | 金丝雀 #1/#2：无埋点派发 10s ✓ → 有埋点派发 10s ✓ → 修正 `matrix_channel` 模块名（CoPaw 将 worker 的 matrix_channel.py 复制进 custom_channels 目录并 `import_module("matrix_channel")` 顶层加载）→ matrix.receive/send span ✓ → 直连探针 HTTP 200（trace `7beffc91a1457180c74167feadcf7469`） |
| 14:59–15:00 | 官方路径 `agt project create elemiso-pr2r3t-gate`（controller 写 meta.json）+ plan.md + 三角色 CASE-MANIFEST；删除过期 INSTRUCTIONS.md（PR3 残留，防串案例）与 5 字节 `matrix_sync_token` ×4（已知问题 #3 预防） |
| 15:00:48 | **第一次重建 4 worker**（`agt update --image`，v2.1 = image `f5d176a339f5`）→ 全部 Running、Matrix 登录、文件同步 ✓ |
| 15:04–15:07 | 启用 OTel ×4（运行时写开关文件，零重启）→ 7 patch ×4 → 4/4 smoke 10s 回复 ✓ → 直连探针 4×HTTP 200 |
| 15:11:34 | kickoff #1（`$W6W7sWCb…`）→ **2 秒后 Leader 报错** `TypeError: object async_generator can't be used in 'await' expression` |
| 15:12–15:27 | 诊断（traceback 指向 `_react_agent.py:685: await self.toolkit.call_tool_function(...)`）：v2.1 的 Toolkit 包装器误写成 async generator 函数，而原函数是"返回 async generator 的协程"。修正（`_wrap_async` 协程接力 + 流式 span 收口），**机制测试改为真实走 Toolkit 调用路径**（真实 agentscope Toolkit + 注册函数 + 未知工具路径全通过）；镜像重建为 v2.2（`8a6b8995ccc2`，同 ID 双 tag `-agentloop`/`-agentloop-v22`） |
| 15:17–15:26 | 金丝雀 #3 卡在 Created：`docker start failed: ports are not available: 0.0.0.0:12693`（Windows 保留端口段 11691-12978 覆盖 controller 随机端口区间约 18%）→ 删除重建即好。**带工具调用**的金丝雀测试：`tool.execute_shell_command` span ✓、回复 CANARY-TOOL-OK-42、导出 SUCCESS |
| 15:27:41 | **第二次（最终）重建 4 worker**（`agt update --image …-v22`）→ 全部 Up（端口 14144/16612/14472/18048 均避开保留段）→ 此后零重启 |
| 15:29–15:34 | OTel ×4 → 7 patch ×4、0 错误 → 4/4 带工具 smoke 10s ✓ → 直连探针 4×HTTP 200 |
| 15:35:31–15:56:00 | **PR #2 R3-TRACED 全链闭环**（20m29s；过程见 PR2 README 事件链） |
| 15:58–16:06 | PR #3 播种（`agt project create elemiso-pr3r2t-reject` + plan + manifest）；发现 worker 无后台周期同步（filesync 为 agent 按需拉取）→ 用容器内 worker 自带 mc（与其 FileSync 同路径）做一次手动拉取（R2 手册"确认同步再 kickoff"纪律），不改容器、不重启 |
| 16:06:40–16:09:15 | **PR #3 R2T 拒绝分支**（2m35s；Reviewer 51 秒完成审查） |
| 16:21:02 | `agt update --state Stopped` ×4 → CR 全部 Stopped，零计费调用者（停机后网关零新调用） |

## 2. 事故与偏差登记（全部如实）

| # | 事件 | 影响 | 处置 |
|---|---|---|---|
| I1 | v2.1 Toolkit 包装器形状错误（async gen vs 协程），导致第一次正式 kickoff（15:11:34Z）失败 | 损失 1 次 kickoff 消息 + Leader 1 轮错误回复（≈2 次 LLM 调用）；项目/DAG 无污染（未达 projectflow） | 如实入包：DM 房间导出含该错误事件；根因修复 + 真实 Toolkit 路径测试 + 金丝雀工具路径验证后才进行第二次 kickoff（15:35:31Z，新事务号避免矩阵幂等去重） |
| I2 | 正式 worker 经历两次容器重建（15:00:48 v2.1 → 15:27:41 v2.2） | 违反"一次创建"字面纪律（目的层面：最终栈自 15:27:41Z 起一次创建零重启，全部正式证据产生于其上）；重建均经 controller 原参 reconcile，非手工 stop/start | 两次重建前均清 matrix_sync_token；每次重建后 Matrix 登录/文件同步/派发 smoke 全部复验通过；PR2/PR3 全部证据在最终容器上产生 |
| I3 | 金丝雀 #3 因 Windows 保留端口段卡在 Created | 金丝雀本身（可弃） | 删除重建即恢复；记录为"agt update/create 重建后必须验证容器 Up"的注意事项 |
| I4 | `agt delete worker canary` 会新建 worker 房间（delete+create ≠ update）；旧房间残留 | 无正式影响（金丝雀独立房间）；正式 worker 全程用 `agt update --image`（房间身份不变，团队房/DM 连续性保持） | 记录 |
| I5 | operator 手动 mc 拉取 PR3 种子文件入容器（15:59Z 一次性） | R2 手册既定纪律（播种→确认同步→kickoff）；与 worker 自身 FileSync 同路径同文件，无内容改动 | 播种文件（PR-METADATA/plan）已在包内，可比对 |
| I6 | Reviewer 具有跨轮记忆（其家目录由 MinIO 持久同步，含 pr2-work/pr2r2-work/pr3-work 历史目录），本轮团队房可见"Source and test confirmed identical to prior runs" | 独立性口径：结论仍由本轮自主 clone/复测得出，但模型见过历史轮结论——与 R1/R2 同一性质，如实披露 | 各 README"独立性范围"小节注明 |
| I7 | GitHub PR state API 匿名限流（403），无法 API 复核 PR open 状态 | 以 `git ls-remote` 分支 SHA 双向一致（1dedf5e1…/ad267a6e…）+ worker 全程无 GitHub 凭据 + 运行前后无任何写操作为零写入佐证 | `github-branches-after.txt` 如实注明 |
| I8 | controller `Stopped` 语义移除 worker 容器与 auth 卷（非仅停止） | "保留容器"字面未达成；CR（Stopped）与 `elemiso-ctrl-data` 数据卷（Matrix/MinIO/CR 全量）保留；全部容器内证据（span/audit/日志）已于停机前实时采集 | 证据时间戳（15:57Z 快照、16:14Z 终采）早于 16:21:02Z 停机 |
| I9 | Reviewer 依赖安装两次超时（pip 大包批量装，PR2 审查拖长至 12 分钟） | 墙钟 +约10 分钟、+十余次 LLM 调用（用量如实统计入包）；未触碰任何护栏 | Reviewer 自行分批安装解决；未注入提示 |
| I10 | 16:18:04Z 一条 Leader 心跳式调用（141K 输入，99% 缓存）发生在 PR3 终报后、停机前 | 计费调用（≈¥0.02-0.1），计入 session 总量；停机后为 0 | 已计入 `usage-summary.json` session 窗口 |

## 3. 预算执行

| 窗口 | 调用 | 输入 tokens（缓存命中） | 输出 tokens | 估算（同 R2 口径） |
|---|---|---|---|---|
| PR2 R3-TRACED | 90 | 6,218,954（98%） | 30,647 | ≈¥1.5–3.3（上界略超 ≤¥2，与 R2 同性质，如实披露） |
| PR3 R2-TRACED | 32 | 2,392,229（99%） | 9,552 | ≈¥0.6–1.3（预算内） |
| 会话合计（15:27Z 起，含 smoke/probe/心跳） | 133 | 8,960,469（99%） | 40,623 | ≈¥2.1–4.6 |

权威数字以用户 DeepSeek 控制台为准。墙钟：PR2 20m29s、PR3 2m35s，均 ≤40 分钟；全程守候无离席空烧。

## 4. 密钥与凭据边界

- AgentLoop license key 仅经 stdin 写入容器开关文件（`enable_otel.py` env 注入），存于工作树外 `D:/mp-finals-tmp/agentloop.key`；未入镜像（镜像内开关文件 `enabled:false` 且无 headers）、未入 Git、未入证据包（证据包经密钥扫描，见各 SHA256SUMS 同目录扫描记录）。
- 证据包不含原始 docker 容器日志（可能含凭据片段），仅含 span/audit、房间导出、MinIO 工件、网关访问日志（无 auth 头）。
- 容器内 `/etc/agentloop-otel.json` 含 key（运行时必要）；容器已由 controller 移除，无残留。
