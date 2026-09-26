# Console 前后端合同矩阵（CONTRACT-MATRIX）

> 2026-09-26 核心能力闭环轮建立。本文件是 console（安全审查工作台）API/数据/页面/状态的
> 唯一对照表：每个用户可见状态必须能追溯到真实后端来源；任何缺口以 G-编号登记，不掩盖。

## 1. API 路由 ↔ 数据源 ↔ 状态 ↔ 页面

| 路由 | 数据源 | 错误/空态 | 消费页面 | 测试 |
|---|---|---|---|---|
| POST /api/auth/login | env 凭据+HMAC | 503 auth_unavailable / 401 / 400 | LoginPage | core-pilot-api.test |
| GET /api/auth/session | 内存会话 | 401 not_authenticated | auth.jsx/Settings | core-pilot-api.test |
| POST /api/auth/logout | 内存会话 | 403 csrf_required | SettingsPage | core-pilot-api.test |
| GET /api/rag/org-search | ORG_RAG_LIVE_URL 代理（flag 门） | 401 / a_chain_disabled / 503 degraded+空结果 | A 链检索 | **contract-states.test（本轮新增）** |
| GET /api/overview | PG live（5s 缓存）或 BACKEND_NOT_WIRED/ERROR | 401；NOT_WIRED/ERROR 诚实 Alert+零值 | OverviewPage | core-pilot-api.test |
| GET /api/pulls | PG skill_receipt_outbox | 401；403 repo_not_in_allowlist；空数组+source | PendingPage/ReposPage/PrDetail | r4-remediation.test |
| GET /api/pending | PG approval.tickets(PENDING) | 401；空数组 | CorePage | core-pilot-api.test |
| GET /api/tickets | PG approval.tickets | 401 | CorePage | core-pilot-api.test |
| GET /api/evidence | PG skill_receipt_outbox | 401 | CorePage | core-pilot-api.test |
| GET /api/audit | PG skill_gate_audit | 401；**audit_table=missing 显式标记（本轮）**；core_source 声明 | CorePage（空态文案区分 missing vs 零决策，本轮） | contract-states.test + e2e-pg |
| GET /api/health | 文件系统+env | 永远 200；data_mode 声明 | 全局 config/Workspace | contract-states.test |
| GET /api/runs(:id)(/evidence·/integrity·/content·/download) | 证据包文件系统 | 404 未知；**404 越权包（存在性不披露）**；400 穿越 | RunsPage/RunDetailPage | api.test/pack.test/r4-remediation.test |
| GET /api/pulls/:number | PG live | 401/403/404 no_live_record；title/head_sha 诚实 null（GitHub 权威未接线） | PrDetailPage | r4-remediation.test（200 长尾→e2e-pg） |
| 其他 /api/* | — | 404 unknown api path | — | core-pilot-api.test |

**已知语义**：错误方法不返回 405，落入 404 兜底（登记为接受的契约语义，非缺陷）。

## 2. PG 表 ↔ 路由（console 为纯读方；FXV 编排表见 FXV-ORCHESTRATION.md）

| 表 | 读方路由 |
|---|---|
| skill_receipt_outbox | /api/pulls、/api/evidence、/api/overview、/api/pulls/:n |
| approval.tickets | /api/pending、/api/tickets、/api/pulls、/api/overview |
| skill_gate_audit（可选表） | /api/audit（缺失→audit_table=missing） |

## 3. 状态补齐记录（本轮 2026-09-26）

| 修复 | 位置 |
|---|---|
| PrDetailPage pg 模式 ReferenceError 崩溃（引用 Contract 变量） | PrDetailPage.jsx head chip |
| RunsPage 筛选统计拉取错误静默吞掉 → 显式降级提示（不回退伪造计数） | RunsPage.jsx facetError |
| CorePage PR/Head 与 Gate 审计表无诚实空态 → 按 source 区分文案；审计表缺失与零决策可区分 | CorePage.jsx + core-pilot.mjs audit_table |
| ApprovalsPage pg 模式零票据无空态 → 诚实零值提示 | ApprovalsPage.jsx |
| OverviewPage 两图空数据渲染空轴 → 空态守卫（NOT_WIRED/ERROR/零值均不冒充） | OverviewPage.jsx |
| PrDetailPage pg 运行详情重试不重取（retry 键错绑外层查询） | PrDetailPage.jsx detailAttempt |
| DiagnosticsPage 接线表过期（/api/pulls 已交付仍标"等待交付"） | DiagnosticsPage.jsx |
| 后端审计表缺失与零决策不可区分 → audit_table 标记 | core-pilot.mjs |

## 4. 登记缺口（未解决，不掩盖）

| 编号 | 缺口 | 状态 |
|---|---|---|
| G-01 | /api/pulls/:n 的 title/current_head_sha 恒 null（GitHub 权威元数据未接线，前端如实显示 —/未记录） | 待 GitHub 只读元数据接线 |
| G-02 | 审批票据 TTL/有效期字段后端响应未携带（approval_expires_at 未投影到 list） | 已知 C-11 |
| G-03 | SettingsPage /api/health 无 loading 指示（字段 — 占位至加载完成） | 低优先 |
| G-04 | console-pg 数据源为开发适配器（fixture:true 明确标注"隔离联调库"），非生产源 | 设计如此 |
| G-05 | 前端无渲染级测试（现有前端测试均为纯模块级） | 登记待办 |
| G-06 | /api/pulls/:n 200 happy path 依赖 live PG（单测不可达），由 e2e-pg 覆盖 | e2e-pg |
