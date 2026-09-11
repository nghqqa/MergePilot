# Phase 14.2H-WD-COPAW-MATRIX-SYNC-AUDIT（只读）审计报告

- 时间窗: 2026-08-29 16:00–16:55 本地（08:00–08:55 UTC）
- 对象: copaw worker Matrix channel 同步环（重启后无法消费既有 delegation @mention / DM event）
- 约束遵守: 全程只读（源码 Read、docker logs、docker exec 内只读 GET/文件读、MinIO mc 读）；
  未修改源码、未构建镜像、未重启容器、未发送任何 Matrix 消息、未改动 PR#2 与任务状态、未输出任何密钥明文、未覆盖 HIGH-RISK 证据。

## 裁决

**COPAW_MATRIX_SYNC_ROOT_CAUSE_IDENTIFIED + COPAW_MATRIX_SYNC_CODE_FIX_REQUIRED**

根因不是单一缺陷，而是两个独立代码级缺陷叠加；二者均可定位到具体文件与函数：

1. **委托 @mention 从未被发送**（上游丢失，`taskflow.py` 平铺任务命名空间 + 幂等复用分支）。
2. **重启重放被 since-token 高水位结构性禁止**（`matrix_channel.py` `_sync_loop`），导致即使重发也只会惠及"未来"消息，历史 event 永远不可达。

---

## 一、关键时间线（全部 UTC，容器日志/事件时间戳）

| 时刻 | 事件 | 证据 |
|---|---|---|
| 03:42:09 | reviewer 容器创建、首次启动；FileSync mirror_all 从 MinIO 拉取 agent 目录；entrypoint "Matrix re-login OK (device: deO5NisqoH)" | docker logs L10-33 |
| 03:59 前后 | manager 发送 sandbox review-1 委托 @mention → team room，event_id=`$nadwji6tQQkl4vSWDktWAFtWs_enjxAcg23927OKmrU` | 房间历史回翻（probe_sync2） |
| 04:01:24 | reviewer 消费 sandbox @mention：`Created queue`→`_consume_with_tracker`（agent 正常运行） | docker logs L167-170 |
| 04:13:17 | unified_queue_manager 空闲清理：`Cleaned up idle queue ... processed=1`（consumer 移除） | copaw.log L111-112 |
| 06:17–06:22 | sandbox 链路后两棒在 team room 正常进行（fix-1 委托 06:17:35、verify-1 委托 06:20:42、verify 完成 06:21:17 = **team room 最后一条文本事件**） | 房间历史（probe_sync3） |
| **06:25:18** | **manager 执行高风险 delegate_task**（meta.json: assigned_at=2026-08-29T06:25:18Z）；manager 侧 agent 会话 06:25:13→06:25:26 仅 13 秒 | meta.json + manager copaw.log L222-234 |
| （同窗） | **team room 无 06:25 之后的任何事件；reviewer DM 无 manager 消息（manager 根本不是该 DM 成员，成员查询 404）** | probe_sync3/4 |
| 07:29:31/42 | run-1 优雅停止（`ChannelManager stopped`），run-2 启动；entrypoint 重新 Matrix re-login（新 device UYSd0XaI0i） | docker logs L196-207, L250 |
| **08:09:06** | 操作员手工重投：@admin 在 reviewer DM `!fai6WChnDcFHxNnuK2` 发送 `[20260829-080906 delegation-redelivery] ...`（event `$_xibtD1CnD5HHzPekXs05X6_TNdXw7ny1L61P_E5o3I`） | DM 历史（probe_sync4） |
| 08:16 / 08:24:40 | run-3、run-4（授权的一次性重启）相继启动；**四次 docker logs 中 04:01 之后再无任何 `Created queue` / `Enqueued` / `Consumer started`** | docker logs 全文 grep |
| 08:37→08:5x | `.copaw/matrix_sync_token` 持续被同步环改写（25196→25466），证明 sync loop 活跃且持续推进高水位 | 容器内文件 mtime + 探测 |

## 二、四个规定审计问题的代码级答案

### 1. sync loop 的启动条件
`copaw_worker/matrix_channel.py`
- `MatrixChannel.start()`（L471）：`channels.matrix.homeserver` 已配置才继续；`whoami()`（或 re-login 后二次 whoami）成功拿到 `_user_id` 才注册回调（L557 `add_event_callback(_on_room_event, (RoomMessageText,))`）；最后 L571 `asyncio.create_task(self._sync_loop())`。
- 部署形态：该文件同时存在于镜像 site-packages 与 `.copaw/custom_channels/matrix_channel.py`（MinIO 同步），两处 sha256 与仓库源码完全一致（555e788e…cdbb74），即运行的就是 223ddc2 忠实构建。

### 2. idle-stop 的触发点（非丢失点）
`copaw/app/channels/unified_queue_manager.py`（框架，镜像内）
- `_cleanup_idle_queues`（L376）：队列**空**且 `last_activity` 距今 > `idle_timeout=600s` → cancel consumer 并移除队列（04:13:17 观测到，`processed=1`）。
- 关键自愈性：`enqueue()`（L119）→ `_get_or_create_queue()`（L165）会在下次消息到达时**重建**队列+consumer。因此 idle-stop 本身不丢消息——04:13 之后的任何成功入队都会留下 `Created queue` 日志，而日志中一次都没有。

### 3. 恢复路径是否存在
**不存在（对早于持久化 token 的 event 结构性不可达）**：
- `_sync_loop`（L701）入口 `_load_sync_token()`（L607）从 `<COPAW_WORKING_DIR>/matrix_sync_token` 恢复高水位；该文件由 filesync push_loop 推 MinIO、容器启动 mirror_all 拉回（entrypoint 日志证实）。
- 有 token：`sync(since=token, full_state=True)`（L742）——homeserver 只返回该位置**之后**的事件。
- 无 token：catch-up sync **清空回调**抑制历史（L705-731 注释明确 "will process messages from next sync"）。
- 全文件无 `/rooms/{id}/messages` 补洞、无 timeline `limited` 处理（grep 证实）；token 保存（L763-764）与下游 consumer 是否真正处理**完全解耦**——sync 成功即推进高水位。

### 4. since token 是否跳过历史 event
**是，实测证实**：用 reviewer 当前 accessToken 发起只读 `GET /sync?since=25466` → `next_batch=25479`，team room 与 DM 均**零事件**返回；而委托时点的事件位于更早流位置。`since=` 之后的重放窗口为空 = 历史已永久跳过。

## 三、根因链

### 根因 A（上游）：委托通知从未产生 —— taskflow 平铺命名空间 + 幂等复用
`copaw_worker/hooks/tools/taskflow.py` `delegate_task`（L440）：
- L446 读 `existing_meta = store.read_task_meta(task_id)`；`FileSystemTaskStore._task_dir` 是**平铺** `shared/tasks/<task_id>/`（task.py），**project_id 不参与路径**。
- 高风险项目复用了 task_id `review-1`，与 sandbox 残留的 `shared/tasks/review-1/`（status=assigned，event_id=03:59 的 `$nadwji…`）**同名碰撞**。
- L458-478 复用分支：`status=="assigned"` 即返回 `sent=True, reused=True` 并把**陈旧 event_id**当作已投递凭证，**不发送任何 Matrix 消息**；即使走 prepared-retry 分支，最终入库的 event_id 仍是陈旧值。
- **存储级铁证**：manager 侧 `shared/tasks/review-1/meta.json` 现为高风险标题 + assigned_at=06:25:18Z，但 event_id=`$nadwji…` 经房间历史核实为 **03:59 sandbox 委托**（正文 "Sandbox review task"，其后有 53 条更新文本事件）；team room 在 06:25 前后零新事件；reviewer DM 中 manager 甚至不是成员（成员态 404）。
- 结论：**reviewer 无可消费对象** ——无论是否重启、无论 channel 是否健康，06:25 的高风险 @mention 在 Matrix 上从未存在。

### 根因 B（下游）：重启重放被 since-token 禁止 —— matrix_channel.py `_sync_loop`
- 唯一"既有 DM event"= 操作员 08:09:06 的 admin 重投（`$_xibtD…`）。
- 内容核实：`m.mentions=null`，body/formatted_body **不含 reviewer MXID**、无 matrix.to 链接（probe_sync4 原文）。
- 08:09 时 run-2 存活（07:29:42–08:16），但无任何入队日志；`_on_room_event`（L1300）所有丢弃点仅 `logger.debug`（未启用）→ 静默丢弃，与日志零痕迹一致。最可疑丢弃点：`is_dm = len(room.users)==2`（L1308）在恢复/全量同步时 room.users 未填充 → 误判群聊 → `_require_mention`（L816）→ `_was_mentioned`（L826）三重检查全部落空（无 m.mentions/无链接/无 MXID）→ 静默入 history。DM allowlist 本身含 @admin（配置已核实），若 DM 判定正确则会入队并留痕——因此日志零痕迹本身就是误判/未收到的佐证。
- 三次重启（07:29/08:16/08:24）均恢复**已越过 08:09** 的高水位 token（run-1 存活至 07:29 且持续保存；后续 run 的 push/pull 经 MinIO 传导）→ `since=` 重放永远跳过该 event。**"重启恢复"在该架构下对历史 event 是不可达路径，重试只能靠"新发送"。**

### 次要发现（不改变裁决，建议一并修复）
- `Re-bridge failed: 'FileSync' object has no attribute 'get_soul'`（run-1 stdout L166）：自定义 channel 重桥接调用不存在的方法，属代码缺陷。
- 每次容器启动 entrypoint 强制 Matrix re-login 产生**新 device**（deO5NisqoH→UYSd0XaI0i→AFfZJt2uBC→junY8PZRSP），长期运行会在 homeserver 累积僵尸 device。
- 平铺 `shared/tasks/` 命名空间还使 manager 侧 meta.json 被跨项目覆写（审计取证时也造成 event_id 误导），是 taskflow 设计级隐患。

## 四、最小修复方案（代码级）

### Fix-1（matrix_channel.py，channel 层，热可替换）
让 token 推进与"投递成功"挂钩，并给重启一个可重放窗口：
1. `_sync_loop` 内：仅当本次响应**不含任何 room timeline 事件**（或所有事件均已 enqueue 成功）时才 `_save_sync_token`；含事件时不推进（或仅推进到"最后一条已 enqueue 事件"的位置）。
2. 启动恢复时对 token 做安全回退：`since = token - REPLAY_WINDOW`（如 30 分钟，Tuwunel 为数值流位置可直接相减），配合**持久化 seen_event_ids 去重集**（追加写文件，入队前查重）防止重复消费。
3. 处理 timeline `limited=true`：用 `/rooms/{id}/messages?dir=f` 从上一次位置补洞后再推进 token。
- 部署面：该文件在 `.copaw/custom_channels/`（MinIO 同步）与 site-packages 双存在且哈希一致；**替换 MinIO 中 custom_channels 文件 + 重启 worker 即生效，无需重建镜像**（site-packages 副本建议同步更新保持一致）。

### Fix-2（taskflow.py，store 层，需改镜像内包）
消除跨项目 task_id 碰撞导致的"假已投递"：
1. 任务目录改为 `shared/tasks/<project_id>/<task_id>/`（或 meta 校验：复用分支必须核对 `existing_meta.project_id == 本次 project_id` 且 event_id 存活，否则视为 fresh/prepared-retry 走真实发送）。
2. `_notify_task_assignment` 返回的 `sent=True` 必须以本次 `_send_matrix_room_message` 的**新 event_id** 为准；`reused=True` 分支不得在未核验事件归属时直接返回成功。
- 部署面：site-packages 内 `copaw_worker-1.0.3` 包文件，**需要重建 copaw-worker 镜像**（或一次性容器内 patch+重启，但不持久、不推荐作为交付态）。

## 五、测试计划（修复后验收）
1. **单测（去重/回退）**：构造 token=25196、seen 集含/不含某 event_id 的用例；断言回退窗口内事件恰好投递一次。
2. **重放闭环（channel）**：worker 停机 → 向其 DM/群 @mention（含正确 m.mentions）→ 等待 > 重启间隔 → 启动 worker → 断言消息被消费且只消费一次（`Created queue` 日志恰好 1 次）。
3. **委托闭环（taskflow）**：项目 A 建 task_id=review-1 完成全链；项目 B 再建 review-1 并 delegate → 断言产生**新** team-room @mention（新 event_id），且 B 的 meta.json event_id ≠ A 的。
4. **误判回归**：空 room.users 场景（模拟恢复期）下发**无 mention** DM 文本 → 若 DM 判定正确应消费；群聊无 mention 应入 history 不投递。
5. **全链回归**：现有 sandbox closed-loop + E2E-FULL 20/20 SHA256SUMS 重跑比对。

## 六、镜像/沙箱影响评估
- **需要重建镜像**：仅 Fix-2（taskflow.py 在 `agentteams/copaw-worker:223ddc2-build1` 的 venv 包内）。Fix-1 可热替换 custom_channels 文件先行验证。
- **沙箱影响**：Fix-1 改变 4 个 copaw worker 的 token 推进/重放行为；去重集保证不重复消费，但需按第五节 2/4 重跑 sandbox 闭环；高风险项目（PR#2）状态与任务存储本次零改动，不受影响。
- **运行时即时恢复（不改代码的临时手段）**：对 reviewer **重发一条含 `m.mentions`/MXID 的新 delegation 消息**即可立即被当前 sync loop 消费（sink 健康已由 04:01 消费与 08:37+ token 推进证明）——这是唯一不需要任何修复即可解锁 HIGH-RISK 人工门的路径。

## 七、证据清单
- 本目录 `AUDIT.md`；只读探测脚本 `probe_sync*.py`/`probe_cfg.py`（GET /sync、GET /rooms/*/messages、配置读取）。
- 容器日志引用：reviewer docker logs（L32/166/167-170/184-185/196-207/246/250/443/632-636）、reviewer copaw.log（L94-112、07:29/08:16/08:24 三段 init）、manager copaw.log（L209-234）。
- 存储引用：manager `shared/tasks/review-1/meta.json`（event_id 陈旧性）、reviewer `openclaw.json`（dm.allowFrom 含 @admin）、`.copaw/matrix_sync_token`（25196→25466）。
- 线上核实：team room 最近文本事件 06:21:17；`$nadwji` 为 03:59 sandbox 委托；DM 唯一消息为 08:09 admin 重投且无 mention；`since=25466` 增量同步零返回。
