# console_pg 隔离 PG 只读 HTTP 联调记录（2026-09-23；第六轮初验 + 第七轮修复后 10/10 复验）

## 复验结论（第七轮，后端 HEAD e82bcfa）

上一轮记录的缺陷 ①② 在后端后续提交中**已修复**（代码核实 + 实测）：

| 缺陷 | 修复确认 |
|---|---|
| ① runs_for_pr 行缺 repo_id → _run_record KeyError | ✅ SELECT/keys 已补 repo_id；`GET /api/runs?repo=X&pr=N` 实测 200 |
| ② except StorageUnavailable NameError（503 语义失效） | ✅ 改为 `except Exception` + `isinstance(conn_err, RuntimeError)` → 503；`StorageUnavailable(RuntimeError)` 基类核实；不可达 DSN 实例实测返回 `503 {"reason":"backend_unavailable"}` |
| ③ 只读连接 idle in transaction（阻塞他窗 DDL） | ⚠️ 未改（pg_runstore `_connect` 仍 autocommit=False）——协调项保留；前端联调已用独立库规避 |

**修复后 10/10 验收全部通过**（记录见 rerun-10of10.log + 截图 05~08）：
①仓库列表（2 仓库/计数来自 PG）②PR 列表 ③PR 详情 ④多 head（PR#9 a2a2a2a2/a1a1a1a1 两行）
⑤多 run（RUNNING/SUCCEEDED 并存）⑥stages/events/evidence（展开面板如实：MinIO 未接线、
findings/validations 数量、合并关闭）⑦跨仓库同编号 #9 不串（other: c1c1c1c1 单 run；
nghqqa: a2/a1 双 run，互不覆盖）⑧分页稳定（/api/prs offset=1 → total 2 items[9]；UI 分页）
⑨不存在 run/PR → 404（curl + UI "PG 服务未返回该 PR 的记录"，不回退快照）
⑩不可达库 → 503 backend_unavailable JSON（curl 实测，错误不冒充空列表）。

---

## 实际连接（首验与复验相同）

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

## 后端缺陷处置状态（更新于第七轮）

1. ~~**`_run_record` KeyError**~~：**已修复**（runs_for_pr 查询补 repo_id 列）；前端仍保留
   repo 级查询 + 客户端过滤的写法（等价结果，非规避缺陷）。
2. ~~**`except StorageUnavailable` NameError**~~：**已修复**（改为 `except Exception` +
   `isinstance(conn_err, RuntimeError)` → 503 backend_unavailable；StorageUnavailable 继承
   RuntimeError 已核实）。注：过宽的 RuntimeError 捕获会把其他 RuntimeError 也归为 503——
   低风险，建议后续收敛为精确类型（非阻塞）。
3. **只读连接 idle in transaction**：未改（autocommit=False 保留）——仅在后端测试窗口
   并发跑 DDL 时构成互相阻塞；前端联调使用独立库 mp_pg_console_fe 规避。

## 未接通（如实）

- 认证（session 401 如实；真实 OAuth 待 D-9）；
- 契约 v2 /api/pulls 聚合形状与 current_head 权威（console_pg 为 console-0.1.0 形状；
  差异已记录，前端 console-pg 适配为**开发/联调适配层**，非第二套正式契约）；
- 审批读写（本轮不带 --approval-dsn/--allow-test-auth，审批 fixture 与真实票据适配器分离）；
- 站内合并（关闭，仅 GitHub 外链）。
