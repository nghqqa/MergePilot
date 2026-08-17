# MergePilot · 可审计、可验证、fail-closed 的合并治理工作流

> **一句话定位**：MergePilot 是一个多 Agent PR 审修与风险治理闭环——把 PR 从"只提意见"推进到受治理的修复、验证、人工审批与回滚，全程留下可审计的结构化证据；高风险变更没有审批就不放行，失败可以回滚，任何环节不确定时**宁可拒绝（fail-closed）也不静默放行**。

**这是什么、不是什么**：这是一个面向工程治理问题的开源项目原型（Apache-2.0），已在隔离栈中完成组件级验证与确定性演示；它**不是**已部署的生产系统，没有真实外部客户，不是已上线的 SaaS/商业化平台，M8 未完成，application/database/production 三项集成验证均为 `false`（见[测试与真实性边界](#八测试与真实性边界)）。

---

## 一、解决什么问题

| 治理问题 | MergePilot 的机制 |
|---|---|
| 受保护分支与受保护路径谁都能改 | Policy Gateway 对每次工具调用做 ALLOW / DENY / ERROR 决策，受保护路径前缀（如 `samples/`）直接拒绝 |
| 高危合并缺少人工把关 | L2 审批票据：受保护合并必须有已批准 ticket 才执行，票据单次有效、CAS 抢占、结果 SHA 绑定 |
| 合并后代码被偷换（revision drift） | 不可变 `revision_bindings` 绑定 approved head SHA；合并后观测 SHA 与批准 SHA 不一致即 `REVISION_DRIFT` 阻断 |
| 出了问题回不去 | rollback / revision-cut 链：合并后验证失败或漂移触发回滚，回滚后复验（re-verify），最终状态可收敛为 `RECOVERED` |
| 出了事说不清 | INSERT-only 审计：`mcp_calls`（网关决策）、`audit_events`（闭环动作）、`rollback_runs`（回滚链）逐行留痕，只读快照可整链回放 |
| 看不见正在发生什么 | 8 页面 ISOLATED_LIVE 只读控制台：从审计库实时读取单一共享快照，overview → evidence 全链可讲 |

设计立场：**LLM 编排本质不可靠**——阶段交接、幂等、崩溃恢复、并发隔离不能交给 Prompt 自觉。MergePilot 用确定性控制面（PostgreSQL 状态机 + Outbox）承载可靠性，Agent 只做语义决策，工具层做权限门。

---

## 二、架构

<details>
<summary>展开完整系统架构图（点图片打开原始 SVG）</summary>
<p>
<a href="docs/showcase/architecture.svg">
<img
  src="docs/showcase/architecture.svg"
  alt="MergePilot isolated live showcase architecture"
  width="960">
</a>
</p>
</details>

- **入口**：PR / 开发者变更请求（演示中由 deterministic seed 模拟，见下方标注）。
- **Policy Gateway**：所有写操作经网关；角色 token 认证 + 写路径约束 + L2 审批票据 + INSERT-only 决策审计；8 类负向场景全部 fail-closed。
- **Controller / MCP orchestration**：PostgreSQL 状态机 + Outbox 是唯一事实来源；事件去重、超时恢复、阶段推进、drift 检测与 rollback 触发都在确定性控制面内。
- **PostgreSQL 审计/快照**：业务表（task_runs/stage_runs/mcp_calls/approvals/rollback_runs/audit_events/revision_bindings）承载事实；`mergepilot_reader` 只读角色经 `REPEATABLE READ READ ONLY` 事务 + 身份/环境/权限/列级 schema 多重校验后输出带完整性摘要的 snapshot。
- **console-edge**：DSN 等秘密只存在于内部网络；这个**无密钥发布桥**是唯一被发布的组件（127.0.0.1 loopback），固定代理单一内部上游，GET 与路径双白名单。
- **8 页面 live console**：单一共享 snapshot、两个只读 GET 端点、单刷新定时器；刷新失败永不回落到静态/伪造数据。

> **四条边界标注**（架构图中同样标出）：
> 1. console-edge 是发布管线（publication plumbing），**不是第五个应用服务**，不构成 application integration；
> 2. deterministic showcase seed 是确定性合成数据——**不是外部客户数据，不是生产证据**，直插 seed 不验证任何 producer 合同；
> 3. **M8-A1 不等于 revision producer integration**（`revision_producer_contract=NOT_VERIFIED`）；
> 4. 生产验证未完成（`application_integration_verified=false`、`database_verified=false`、`production_verified=false`）。

本图展示隔离演示拓扑（内部后端网络 + loopback 发布），不是生产部署、云架构或多租户系统。

---

## 三、三个确定性演示案例

三个案例由 `tools/demo_console/showcase_cases.py` 以**确定性 seed** 注入审计库（固定时间戳、固定 SHA、INSERT-only、重放幂等），经真实只读管线在 8 页面中从头讲到尾。

> **Deterministic showcase seed — not external customer data — not production evidence**

### Case A · Protected Merge Success（`case-showcase-protected-merge-success`）

| 项 | 值 |
|---|---|
| run_id | `run-showcase-a` |
| PR / 分支 | #101 · `fix/showcase-a` → `main`（`mergepilot/showcase-demo`，合成仓库） |
| base / head SHA | `73686f77636173652d612d626173650000000000` / `73686f77636173652d612d686561640000000000` |
| merge SHA | `73686f77636173652d612d6d6572676500000000` |
| 阶段序列 | review → fix → verify（PASS）→ merge（MERGED） |
| Policy Gateway | INTENT `ALLOW`（`POLICY_PASS_L2_APPROVED`）→ RESULT `ALLOW`（`L2_TICKET_APPROVED`） |
| L2 / 审计证据 | ticket `tkt-showcase-a-l2`；audit_events 五步闭环（review/fix/verify/merge/close_pr） |
| 最终状态 | **MERGED**（无 DENY、无回滚、无失败原因） |

截图：[overview](docs/showcase/presentation/desktop-01-overview@2x.png) · [timeline](docs/showcase/presentation/desktop-02-timeline@2x.png) · [trace](docs/showcase/presentation/desktop-05-trace@2x.png) · [rag](docs/showcase/presentation/desktop-04-rag@2x.png)

### Case B · Fail-Closed Policy Rejection（`case-showcase-failclosed-policy-rejection`）

| 项 | 值 |
|---|---|
| run_id | `run-showcase-b` |
| PR / 分支 | #102 · `fix/showcase-b` → `main` |
| base / head SHA | `73686f77636173652d622d626173650000000000` / `73686f77636173652d622d686561640000000000` |
| 拒绝原因 | 写入受保护路径前缀 `samples/` —— Policy Gateway `DENY`（`PROTECTED_PATH_PREFIX`，`create_or_update_file`） |
| 阶段序列 | review（COMPLETED）→ fix（**FAILED / DENIED**）——时间线在拒绝点终止，无 verify/merge 阶段 |
| 失败原因（run） | `POLICY_DENY: write to protected path prefix (samples/); run blocked before merge (fail-closed)` |
| 审计证据 | audit_events 含 `policy_deny` 记录；无 merge SHA、无伪造审批（L2 状态符合真实拒绝流程） |
| 最终状态 | **FAIL**（拒绝语义；`FAIL` 为审计库合法枚举） |

截图：[findings](docs/showcase/presentation/desktop-03-findings@2x.png) · [mobile timeline](docs/showcase/presentation/mobile-02-timeline@2x.png)

### Case C · Revision Drift Recovery（`case-showcase-revision-drift-recovery`）

| 项 | 值 |
|---|---|
| run_id | `run-showcase-c` |
| PR / 分支 | #103 · `fix/showcase-c` → `main` |
| approved head SHA | `73686f77636173652d632d686561640000000000` |
| merge / drifted / recovered SHA | `73686f77636173652d632d6d6572676500000000` / `73686f77636173652d632d647269667400000000` / `73686f77636173652d632d7265636f7665726564`（四 SHA 互不相同） |
| 漂移检测 | 合并后观测 head ≠ 批准 head → `get_pull_request` `DENY`（`REVISION_DRIFT`，fail-closed） |
| 回滚 | `rb-showcase-c-1`：revision-cut 回滚 reverted merge SHA → revert 结果即 recovered SHA，re-verify `PASS` |
| L2 / 审计证据 | ticket `tkt-showcase-c-l2`；audit_events 含 `drift_detected` 与 `rollback` 记录 |
| 最终状态 | **ROLLED_BACK**（`rollback_runs.status=RECOVERED`） |

截图：[safety](docs/showcase/presentation/desktop-06-safety@2x.png) · [evidence](docs/showcase/presentation/desktop-07-evidence@2x.png) · [mobile safety](docs/showcase/presentation/mobile-03-safety@2x.png) · [mobile evidence](docs/showcase/presentation/mobile-04-evidence@2x.png)

---

## 四、8 页面控制台（Desktop 1440×900）

以下画廊显示的是 **高 DPI presentation 副本**（@2x，deviceScaleFactor=2 重新真实渲染截取，仅为清晰展示；点击图片打开对应的 **canonical 验证截图**（1440×900 / 390×844，真实验证资产）。

所有截图均来自**真实六容器栈**的 live 快照（banner=OK、placeholder=0、内容与 seed 精确一致），数据为 deterministic showcase 数据：

| 页面 | 用途 | 截图案例 / run_id | 截图 |
|---|---|---|---|
| 01 overview | 运行身份：case 徽章、run_id、PR、head SHA、最终状态 | Case A · `run-showcase-a` | [![desktop-01-overview](docs/showcase/presentation/desktop-01-overview@2x.png)](docs/showcase/screenshots/desktop-01-overview.png) |
| 02 timeline | 按 `started_at` 排序的完整阶段链与 verdict | Case A（review→fix→verify PASS→merge MERGED） | [![desktop-02-timeline](docs/showcase/presentation/desktop-02-timeline@2x.png)](docs/showcase/screenshots/desktop-02-timeline.png) |
| 03 findings | 内联发现 + 网关拒绝事实（来自 `mcp_calls` 审计） | Case B（DENY / PROTECTED_PATH_PREFIX / fail-closed 详情） | [![desktop-03-findings](docs/showcase/presentation/desktop-03-findings@2x.png)](docs/showcase/screenshots/desktop-03-findings.png) |
| 04 rag | RAG 咨询边界：真实 `not_measured`，不伪造结论 | Case A（advisory-only、not adopted、untrusted） | [![desktop-04-rag](docs/showcase/presentation/desktop-04-rag@2x.png)](docs/showcase/screenshots/desktop-04-rag.png) |
| 05 trace | 决策与执行链（网关审计行） | Case A（INTENT/RESULT ALLOW + L2 ticket + merge SHA） | [![desktop-05-trace](docs/showcase/presentation/desktop-05-trace@2x.png)](docs/showcase/screenshots/desktop-05-trace.png) |
| 06 safety | Policy Gateway 汇总 + 回滚详情（reverted/recovered SHA、re-verify） | Case C（RECOVERED、reverify PASS） | [![desktop-06-safety](docs/showcase/presentation/desktop-06-safety@2x.png)](docs/showcase/screenshots/desktop-06-safety.png) |
| 07 evidence | bundle 完整性、audit 摘要、L2 审批记录 | Case C（drift_detected×1、tkt-showcase-c-l2） | [![desktop-07-evidence](docs/showcase/presentation/desktop-07-evidence@2x.png)](docs/showcase/screenshots/desktop-07-evidence.png) |
| 08 benchmark | 诚实的能力边界：`NOT_MEASURABLE_WITH_CURRENT_RUNTIME` | 真实边界（不虚构性能数字） | [![desktop-08-benchmark](docs/showcase/presentation/desktop-08-benchmark@2x.png)](docs/showcase/screenshots/desktop-08-benchmark.png) |

## 五、Mobile 布局（viewport 390×844）

以下截图用于证明展示布局在移动视口下的适配（单列布局、长 SHA/原因文本换行不撑宽页面）：

| 页面 | 案例 | 截图 |
|---|---|---|
| overview | Case A · `run-showcase-a` | [![mobile-01-overview](docs/showcase/presentation/mobile-01-overview@2x.png)](docs/showcase/screenshots/mobile-01-overview.png) |
| timeline | Case B（时间线终止于拒绝） | [![mobile-02-timeline](docs/showcase/presentation/mobile-02-timeline@2x.png)](docs/showcase/screenshots/mobile-02-timeline.png) |
| safety | Case C（recovered SHA 可读） | [![mobile-03-safety](docs/showcase/presentation/mobile-03-safety@2x.png)](docs/showcase/screenshots/mobile-03-safety.png) |
| evidence | Case C（L2 记录可读） | [![mobile-04-evidence](docs/showcase/presentation/mobile-04-evidence@2x.png)](docs/showcase/screenshots/mobile-04-evidence.png) |

> 不声称完整 WCAG 合规。键盘导航、reduced-motion 与浏览器控制台监控的 **residual validation 限制**按 PR-V1 披露如实保留（受演示环境浏览器自动化工具能力所限，未自动化验证），不升级为无障碍认证。

---

## 六、Quick Start（隔离栈 · 无生产依赖）

### 前置条件

- Windows + WSL2（`MergePilot-Test` 发行版内运行 Docker daemon；本仓库测试与编排脚本只认 `unix:///var/run/docker.sock`）
- Python 3.12（宿主）；容器镜像基于 `python:3.12-slim` 与 digest 固定的 `pgvector/pgvector`
- 需要真实 PostgreSQL 的测试仅在 `EPHEMERAL_PG_VERIFY=1` 时执行（平时保持 unset）

### 构建 + 启动（one-click 编排）

所有 docker 操作都由 `tools/demo_console/one_click_startup.py` 的编排计划函数生成（argv 数组、`shell=True` 禁用、密钥只经 0600 secret 文件传输，绝不进 argv/日志）：

```bash
# 构建五个本地服务镜像（仓库根目录）
docker build -f Dockerfile.policy-gateway -t mergepilot-isolated-policy-gateway:local .
docker build -f Dockerfile.controller     -t mergepilot-isolated-controller:local .
docker build -f Dockerfile.demo-console   -t mergepilot-isolated-demo-console:local .
docker build -f Dockerfile.console-edge   -t mergepilot-isolated-console-edge:local .
docker build -f Dockerfile.preflight      -t mergepilot-isolated-preflight:local .

# 生成完整启动计划（网络→postgres→gateway→controller→demo-console→edge→preflight）
python - <<'PY'
from tools.demo_console.one_click_startup import plan_orchestrated_start
plans = plan_orchestrated_start(
    env_file="<postgres.env 路径>",               # SecretFile 写入（含你生成的随机密码）
    controller_env_file="<controller.env 路径>",   # ControllerSecretFile 写入
    reader_dsn_env_file="<demo_console.env 路径>", # ReaderDsnSecretFile 写入
    demo_console_run_id="run-showcase-a",          # 案例选择：run-showcase-a/b/c
    demo_console_pg_server_addresses="<postgres 容器桥接 IP>")
for argv in plans:
    print(argv)   # 依次执行并等待各 healthcheck
PY
```

密钥文件中的值一律使用你自己生成的随机值（例如 `python -c "import secrets; print(secrets.token_urlsafe(18))"`）；**本 README 不包含任何真实密码、DSN、token 或生产地址**，示例值均为占位符或公开 synthetic 值。

### 初始化数据库 + 注入 deterministic showcase seed

```bash
# 依次应用 tools/audit-db/ 迁移（13 次幂等应用）与 tools/demo_console/migrations/（reader ACL）
# 然后注入种子（psql 经 stdin，--single-transaction 原子应用）：
python tools/demo_console/showcase_cases.py | \
  docker exec -i <postgres 容器> psql -U mergepilot -d mergepilot_audit \
    -v ON_ERROR_STOP=1 --single-transaction -f -
```

seed 为 INSERT-only 且重放幂等（audit_events 以 8 列 `NOT EXISTS` + `IS NOT DISTINCT FROM` 守卫）。

### 访问与检查

- 控制台（经 console-edge，仅 loopback）：`http://127.0.0.1:8600`
- 切换案例：以 `run-showcase-a` / `run-showcase-b` / `run-showcase-c` 重建 demo-console 容器（改 `MERGEPILOT_RUN_ID` 环境变量）
- 启动门禁：preflight 容器输出 `PREFLIGHT_OK`（10 门：守护进程身份/镜像摘要/健康/连通/身份/环境标记/reader ACL/只读事务/来源类型/HTTP 端点）

### 清理

```bash
python - <<'PY'
from tools.demo_console.one_click_startup import plan_orchestrated_cleanup
for argv in plan_orchestrated_cleanup():
    print(argv)   # 逆序 rm 容器 + 删除两个网络；随后删除 secret 文件并 docker volume prune
PY
```

### 快速跑测试（不需要 Docker）

```bash
python -m pytest tests/demo_console tests/isolated_live tests/verification -q --import-mode=importlib
bash tests/skills/run_all.sh       # 6 Skill 确定性测试
bash tests/m4f1/run_all.sh         # AgentTeams 协议级 E2E（fixture）
```

---

## 七、演示脚本

5 分钟演示流程见 [`docs/showcase/demo-script.md`](docs/showcase/demo-script.md)——与上述页面、案例与截图一一对应。

---

## 八、测试与真实性边界

### 测试数字（全部可复现）

| 项 | 数字 |
|---|---|
| PR-V1 视觉系统测试（`test_dynamic_refresh.py`，含 25 项设计系统用例） | 81 passed |
| PR-V2 deterministic cases 测试（`test_showcase_cases.py`，60 项：案例合同/唯一性/确定性/真实引擎重放幂等/SQL 范围/渲染转义/引擎不变量/fail-closed 变异） | 60 passed |
| 当前完整回归（demo_console + isolated_live + verification；ResourceWarning-as-error） | **1195 passed / 13 skipped / 0 failed** |
| F1 audit seed 重放（真实 PostgreSQL + SQLite 双重实证） | showcase audit 行 **12 → 12**、task_runs 3 → 3、按 action 摘要不变 |
| F2 recovered SHA | API snapshot / Desktop 1440×900 / Mobile 390×844 三侧可见 |
| 真实组件证据 | 5 个长运行服务 healthy + preflight 完成即 **PREFLIGHT_OK（10/10 门）**；三案例经 console-edge `status`/`snapshot` HTTP 200 且 run_id/PR/SHA/状态与 seed 精确一致 |

### 真实性边界（未提升，逐项可核）

- showcase seed 是 **deterministic synthetic data**：不是外部客户数据，不是生产证据；
- `application_integration_verified=false`；
- `database_verified=false`；
- `production_verified=false`；
- `revision_producer_contract=NOT_VERIFIED`；
- `audit_producer_contract=NOT_VERIFIED`；
- **M8-A2 未实现**；M8-A1（事件摄取机制）不等于 revision producer integration；
- 不声称生产部署、真实用户或任何生产性能。

---

## 证据与文档

- [`docs/初赛证据索引.md`](docs/初赛证据索引.md) — 按声明定位证据
- [`docs/初赛声明-证据矩阵.md`](docs/初赛声明-证据矩阵.md) — 逐项声明 vs 限制（唯一措辞权威）
- [`docs/项目状态.md`](docs/项目状态.md) · [`docs/复赛路线图.md`](docs/复赛路线图.md)
- [`benchmark/formal-summary.md`](benchmark/formal-summary.md) — Benchmark 冻结结论（受控本地评测，不外推）
- [`docs/README-历史运行记录.md`](docs/README-历史运行记录.md) — 开发期排障与旧 Demo 路径（归档）

## 仓库结构

```
MergePilot/
├── README.md                  # 本文件
├── LICENSE                    # Apache 2.0 · THIRD_PARTY.md 依赖与数据边界
├── config/ skills/            # team.yaml + 6 Skill DAG（确定性子进程）
├── tools/
│   ├── policy-gateway/        # 最小权限网关（L2 审批、INSERT-only 审计）
│   ├── workflow-controller/   # 状态机 / Outbox / m4f 事件机制
│   ├── audit-db/              # PostgreSQL 迁移（保护路径）
│   └── demo_console/          # 只读快照源 + serve + console-edge + showcase seed
├── tests/                     # skills / m4f1 / m5_0 / demo_console / isolated_live / verification …
├── benchmark/                 # 冻结评测数据与产物
├── evidence/                  # 机器可验的历史运行证据（按里程碑，冻结）
├── samples/                   # 样例 PR / fixture（保护路径）
└── docs/                      # 设计、状态、showcase 材料（docs/showcase/）
```

## 团队与许可

队伍「分子」· 邱全安（队长，架构 / Agent 编排 / 风险门）· 彭明（Skill 与 MCP / Demo）· 何斌（基础设施 / 可观测 / 文档开源）。
[Apache License 2.0](LICENSE)。本作品为 [GOAI Agent Infra 赛道](https://www.goaihz.com/tracks?track=infra)参赛项目。截图与图表均为本项目自有产物（无第三方图片/字体依赖）。
