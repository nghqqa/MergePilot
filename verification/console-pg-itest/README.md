# console_pg 隔离 PG 只读 HTTP 联调记录（2026-09-23）

## 实际连接

| 项 | 值 |
|---|---|
| 前端入口 | http://127.0.0.1:4194（dev harness `--pg` 模式：静态 dist + 可信配置 + /pg 反代） |
| 后端只读服务 | console_pg v0.2.0-pg-approval @ http://127.0.0.1:4193（`dev/console-pg-launch.py` 启动；仅 127.0.0.1） |
| 数据 | 隔离 PG 实例 127.0.0.1:55432 / db=**mp_pg_console_fe**（独立于后端测试库，避免 schema drop 竞争）；4 条 fixture run 经**后端 PgRunStore API** 写入（create_run/save_stages/transition_status/append_event），迁移经后端 apply_migrations.py 应用 |
| 数据性质 | data_mode=fixture（隔离测试记录）——**非真实运行、非真实 PR 审查完成** |

## 实际请求/响应路径（前端经适配层 data/sources.js consolePgSource）

- `/pg/api/repos` → `{items:[{repo_id,pr_count,run_count,latest_activity}],data_mode:"fixture"}`
- `/pg/api/prs?repo=owner%2Fname` → `{total,limit,offset,items:[{repo_id,pr_number,head_sha(MIN),run_count,latest_status,latest_activity}]}`
- `/pg/api/runs?repo=...` → `{items:[RunRecord(含 mode/status/outcome/superseded_by_run_id)]}`（**规避** `/api/runs?repo&pr` 组合缺陷：适配层 repo 级查询 + 客户端 pr_number 过滤）
- `/pg/api/runs/:runId` → 详情（stages/events/findings/validations/evidence/merge_panel）
- `/api/auth/session` → 401 not_authenticated（认证未实现，如实透传）

## 已验证（浏览器 + curl，截图 harness-*.png 与 01–04）

1. 仓库列表（2 仓库，pr/run 计数来自 PG）✓
2. 仓库内 PR 列表（PR#9 run_count=2、PR#10=1）✓
3. PR 详情（nghqqa#9：运行历史 2 条）✓
4. 多 head（PR#9 两行 head a2a2a2a2 / a1a1a1a1）✓
5. 多 run（2 run，含 RUNNING 与 SUCCEEDED 并存）✓
6. run stages/events/evidence（展开面板：review:generic RUNNING attempts1；3 事件；evidence/MinIO 未接线如实）✓
7. 跨仓库同编号 PR #9 不串（acme/other: c1c1c1c1 单 run FAILED；nghqqa: a2/a1 双 head 双 run）✓
8. 分页稳定（/api/prs total/limit/offset + 前端分页互不干扰）✓
9. 404（GET /api/runs/NOPE → 404 run_not_found；前端显示"不回退历史快照"）✓
10. 503：**当前后端代码无法返回**——见下方缺陷②（已精确复现并记录）

## 界面诚实性

- 顶栏 "PG 只读 · Fixture（未认证）" + 会话区 "PG Fixture · 未认证" 常驻；
- 详情页 "PG 只读 · Fixture（隔离测试记录，非真实运行）" chip + note；
- PG 读模型无 verdict/current_head 权威 → 页面显示"结论未记录"，
  **不显示当前结论、不从历史 HIGH/APPROVED 生成待办**（待处理页在非 snapshot 源显示说明而非汇总）。

## 后端需修正（tools/console_pg/server.py @ 18586cc，已精确复现）

1. **`_run_record` KeyError**：`GET /api/runs?repo=X&pr=N`（runs_for_pr 路径）的行缺
   `repo_id` 键 → `_run_record` KeyError → 连接重置（curl 000）。
   修复建议：runs_for_pr 查询补 repo_id 列，或 `_run_record` 用 `r.get("repo_id")`。
2. **`except StorageUnavailable` NameError**：该名未在 server.py 定义/导入——
   PG 不可达时（StorageUnavailable 正常抛出）503 语义被 NameError 替换成连接重置。
   复现：任意 --dsn 指向不可达端口后 GET /api/repos。
   修复建议：`from orchestrator_v3.pg_runstore import StorageUnavailable`（与调用方一致加载）。
3. **共享 fixture 库竞争**（协调项）：console_pg 连接空闲持有事务
   （pg_runstore 默认非 autocommit），会阻塞后端测试窗口的 `DROP SCHEMA run CASCADE`；
   建议只读服务连接使用 autocommit（或专用只读库）——前端联调已改用独立库
   mp_pg_console_fe 规避。

## 未接通（如实）

- 认证（session 401 如实；真实 OAuth 待 D-9）；
- 契约 v2 /api/pulls 聚合形状与 current_head 权威（console_pg 为 console-0.1.0 形状；
  差异已记录，前端 console-pg 适配为**开发/联调适配层**，非第二套正式契约）；
- 审批读写（本轮不带 --approval-dsn/--allow-test-auth，审批 fixture 与真实票据适配器分离）；
- 站内合并（关闭，仅 GitHub 外链）。
