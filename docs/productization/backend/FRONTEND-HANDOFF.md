# 后端 → 前端交接（FRONTEND-HANDOFF）

**日期**：2026-09-22 ｜ **后端提交**：`feat/backend-pg-storage` @ `0500a12`+ ｜ **设计基线**：`docs/architecture-audit-20260922` @ `caf6909`

## 1. 数据现在在哪

| 存储 | 内容 | 说明 |
|---|---|---|
| PostgreSQL（隔离实例 127.0.0.1:55432，db=mp_contract） | `run.targets / run.runs / run.stages / run.run_events` + `approval.tickets / approval.ticket_audit` | 结构化业务数据（设计契约 caf6909；V0 默认运行路径仍未切换，仅受控使用） |
| SQLiteTicketStore / RunStore(SQLite) | 既有票据与 v3 run 记录 | 迁移源；TicketStore 契约测试已抽取（tests/approval/store_contract.py），PG 实现通过同一套 |
| MinIO | run-manifest、阶段计划、证据 | 不变 |

## 2. 可用查询接口（当前）

- `PgRunStore.get_run(run_id)` → run 全字段（含 target/chain/class/exec_seq/status/outcome）；
- `PgRunStore.runs_for_pr(repo_id, pr)` → 按 created_at DESC 的 run 列表；
- `PgRunStore.get_stages(run_id)` → {stage: {status,attempts,error,...}}；
- `console_contract.build_console_payload(...)` → 控制台字段契约（reviewers/coverage/degradations/manifest/rag）——PG 记录经 `_load_run` 形状直接可喂；
- SQLite 侧：`RunStore.get_run/list_runs/runs_for_pr`、`GET /api/runs`（console_v3 server，只读）。

**尚无正式 HTTP 接线到 PG**——当前交付的是存储层与读模型函数；"前端已接通实时数据"不成立。

## 3. 身份关联（重要语义）

- `target_id = 'tgt-' + sha256(canon(repo,pr,head))[:20]`，UNIQUE(repo,pr,head)：同 head 多次投递收敛同一 target；
- `run_id = 'gh-' + sha256(canon(chain,class,repo,pr,head,seq))[:24]`：同 target 可多条 run（legacy 执行 + v3 shadow 取证并存；显式重跑 seq+1）；
- `runs.request_key` UNIQUE：webhook 首 run=`'<delivery_id>:<chain>:<class>'`；重跑=调用方 rerun token；
- ticket↔run：`approval.tickets.run_id` → run；activity 唯一 = (run_id, action, target_key)。

## 4. 展示语义（红线）

1. **mode 必须显示**：legacy/shadow/fixture/on 是不同性质的记录，不得合并展示为"真实运行"；
2. **最新 run ≠ 当前 head 已完成审查**：以 `runs.status` 为准；`superseded_by_run_id` 非空表示已被新执行取代；
3. **coverage.complete=false 或 outcome=MANUAL_ATTENTION/REVIEW_PARTIAL** 不得渲染为绿色成功（契约测试固化）；
4. **部分字段可为 NULL**：risk_tier（diff 不可得时降级）、outcome 相关字段——如实展示缺失，不填占位结论；
5. v3 shadow run 的 reviewer 维度为 SKIPPED("agent not executed")——不是"已审查通过"。

## 5. 分页与错误语义

- 列表查询当前全量返回（list_runs limit=50 默认）；分页/游标为后续工作项；
- 错误语义：404=run 不存在；405=写方法被拒（控制台 GET-only）；StorageUnavailable=后端存储不可达（展示"后端不可用"，不伪装空列表）。

## 6. 可复现测试数据

- 门控：`MERGEPILOT_PG_CONTRACT=1 python -m pytest tests/approval/test_store_pg.py tests/orchestrator/test_pg_runstore.py`——在隔离 PG(55432) 上生成确定性 target/run/stage 数据（run_id 由设计 §3.2 规范派生，可复算）；
- shadow/fixture 语义样例：tests/orchestrator/test_adapter_vertical.py 的 fixture 管道。

## 7. 后续（后端窗口将交付）

- findings/validations/stage_attempts 的 PG 落库（设计已给形状）；
- findings 聚合结果的 JSONB 查询面；
- 正式 HTTP 只读 API（当前 console_v3 server 读 SQLite RunStore，PG 版随统一迁移切换）。
