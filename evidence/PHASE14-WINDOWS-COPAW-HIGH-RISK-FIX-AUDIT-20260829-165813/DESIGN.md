# WD-COPAW-HIGH-RISK-FIX-AUDIT — 存储路径与同步语义设计（审计先行）

阶段: Phase 14.2H-WD-COPAW-HIGH-RISK-FIX-AUDIT
前置: PHASE14-WINDOWS-COPAW-MATRIX-SYNC-AUDIT-20260829-164840（根因 A/B）
约束映射: 不改 OpenClaw 主线 / 不改 copaw-sandbox / 仅高风险 runtime 或新隔离 sandbox 验证 /
不改 PR#2 业务代码 / 不发新 kickoff / 验证完成前不派发 Fixer/Verifier / 不落 Secret / 旧证据只读。

## 一、taskflow 存储路径设计审计（现状）

| 触点 | 现状 | 问题 |
|---|---|---|
| `task.py` `FileSystemTaskStore._task_dir` | `shared/tasks/<task_id>/`（平铺） | project_id 不参与路径 → 跨项目同名 task_id 物理冲突 |
| `taskflow.py` `delegate_task` L453/446 | `task_path=f"shared/tasks/{tid}/"`；复用分支按 tid 读 meta | 复用分支把**其他项目**的 assigned meta（含陈旧 event_id）当作已投递凭证 |
| `taskflow.py` check/ack/submit L560-602 | pull/push 平铺路径；read_task_meta 仅按 tid | worker 侧同样暴露于跨项目覆写 |
| `projectflow.py` L251/254/275/281 | taskPath 平铺；read 不带 project | leader 审计视图跨项目串号 |
| `task.py` `validate_task_result` L952 | deliverables 必须在 `shared/tasks/<tid>/` 下 | 新布局需同步放行 |
| MinIO 同步 | 整目录 mirror（agents/<w>/ 与 shared/ 双向） | 新布局自动随 mirror 传播，无需改 sync.py |

## 二、Fix A 设计（project-scoped task 存储）

**新规范布局**: `shared/projects/<project_id>/tasks/<task_id>/{meta.json,spec.md,result.md}`
（项目工件全部收敛到既有项目目录下；MinIO 前缀天然按项目隔离）

**兼容语义（不迁移、不触碰 sandbox 旧数据）**:
- 写入：一律 project-scoped（`meta.project_id` 决定路径）。旧平铺目录不再被写入。
- 读取解析顺序 `read_task_meta(task_id, project_id=None)`:
  1. 显式 project：`shared/projects/<pid>/tasks/<tid>/meta.json`，缺失时回退平铺路径，但**强校验** `meta.project_id == <pid>`（防跨项目读取）；
  2. 无显式 project：扫描 `shared/projects/*/tasks/<tid>/` 全部候选 + 平铺候选；
     - 0 候选 → TaskflowError(not found)；
     - 多项目候选 → TaskflowError(ambiguous, 提示传 projectId)；
     - 同项目多布局命中 → 优先 project 布局。
- spec/result 读写同规则；`validate_task_result` 放行 `shared/tasks/<tid>/` 与 `shared/projects/<pid>/tasks/<tid>/` 两种前缀。
- `prepare_task` / `commit_task_assignment` 的内部读全部改为带 project_id 的作用域读。
- taskflow `delegate_task` 复用分支双重护栏：`existing_meta.project_id == 本次 project_id` **且** `event_id` 非空才允许 reused=True。
- sync pull/push：delegate 用新路径；ack/check/submit 先本地解析 meta（容器启动已整目录 mirror），再对解析出的布局路径做**容错式** pull（远端缺失不致命）与 push（写到 meta.project_id 对应新路径）。

## 三、Fix B 设计（Matrix 委派 mention 与历史恢复语义）

文件：`copaw_worker/matrix_channel.py`

1. **幂等去重账本**: `<wd>/matrix_seen_events`（追加写，cap 10000 行截断）。`_on_room_event`/媒体回调在 `_enqueue(payload)` 成功后记录 event_id；入口先查账本，命中即丢弃 → 重放恰好一次。
2. **重放窗口**: `_load_sync_token` 对数值型 token（Tuwunel 流位置）回退 `AGENTTEAMS_MATRIX_REPLAY_EVENTS`（默认 200，可 0 关闭）个位置；重启后窗口内历史事件由 homeserver 重投，配合账本实现 exactly-once。非数值 token 保持原语义（不回退）。
3. **DM 判定加固**: `is_dm = len(room.users)==2` 在恢复/全量同步期 room.users 可能为空（len=0 被误判群聊 → mention 必需 → 无 mention 文本被静默丢弃）。修复：len==0 时经 `_client.joined_members(room_id)` 实查成员数，查询失败按 False 并留 WARNING。
4. **mention MXID 过滤**：`_was_mentioned` 三层（m.mentions user_ids / matrix.to 链接 / 全文 MXID 正则）保持不变，测试锁定；DM 路径不需要 mention（`_check_allowed` DM allowlist 生效）。

## 四、验证矩阵（阶段规定八项）

| # | 测试 | 层 | 断言 |
|---|---|---|---|
| T1 | 隔离性 | store 单测 | projA/projB 同名 review-1 写入不同目录，互不可见 |
| T2 | 复用分支 | taskflow 工具单测 | 异项目 assigned meta → 不复用、必发送；同项目 assigned+event_id → 复用不发送；同项目 assigned 无 event_id → 不复用走发送 |
| T3 | 跨项目同 task_id 回归 | taskflow 工具单测 | projB delegate 产生新 event（notify 调用计数），meta 各自隔离 |
| T4 | token 推进 vs enqueue | matrix 单测 | token 已推进时事件因回放窗口仍重投；enqueue 失败不记账本、成功才记账本 |
| T5 | mention MXID 过滤 | matrix 单测 | m.mentions/链接/正文 MXID 三层命中；群聊无 mention 落 history；DM 无 mention 且 sender 在 allowlist → 投递 |
| T6 | since 回放 | matrix 单测 | 数值 token 25396 → 恢复为 25196（窗口 200）；0 可关闭；非数值不变 |
| T7 | 重复/幂等 | matrix 单测 | 同 event_id 二次回调不再 enqueue；账本跨"重启"（重新 load）仍生效 |
| T8 | DM 判定 | matrix 单测 | room.users 空 → joined_members 实查=2 → DM；=3 → 群聊；查询异常 → False |

单测运行环境：build1 镜像派生的**一次性 scratch 容器**（新隔离 sandbox，满足约束 3），源码以副本注入，不触碰 4 个在役容器。
实弹验证：仅高风险 runtime 的 manager+reviewer 升级 build2 后按阶段成功条件推进。

## 五、成功条件 ↔ 机制对照

1. 不同 project 的 review-1 不再冲突 → Fix A 路径隔离 + T1/T3
2. delegate_task 必须产生新的有效 event → Fix A 复用护栏 + T2/T3（notify 必调用）
3. event 包含目标 Reviewer MXID mention → `_notify_task_assignment` 构造 m.mentions+正文 MXID（既有实现），实弹 GET 验证
4. Reviewer 能消费 review-1 → Fix B 回放/DM 加固 + 实弹消费验证
5. HIGH_RISK_FOUND 输出后才允许人工门/Fixer/Verifier → 本阶段止步于 Reviewer result.md，不含派发动作
