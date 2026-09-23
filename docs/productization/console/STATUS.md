# 控制台推进状态（console/STATUS）

**更新**：2026-09-23（第八轮：test-auth 审批浏览器联调完成）｜ **分支**：`feat/admin-console`（worktree `D:\goai\mp-worktrees\console`，基线 `0ae8843`）
**负责目录**：`console/`（前后端）+ `docs/productization/console/`。未改动 gh-bridge / workflow-controller / approval 状态机本体 / rag / costmeter / 共享数据结构 / r3work。

## 第八轮：test-auth 审批联调（2026-09-23）

console_pg 0.2.0 已带审批只读+决策端点（POST /api/approvals 创建、/:id/approve|reject 决策、
GET 列表/详情；--allow-test-auth + X-Test-Principal 隔离主体）。本轮在独立 PG（mp_pg_console_fe）
上完成**真实页面路径的 test-auth 审批浏览器联调**——票据为隔离 fixture 库合成数据，**非真实票据、
非真实 GitHub 操作；批准只生成后续动作意图**。

| 工作项 | 状态 | 说明 |
|---|---|---|
| 审批端点核实 | ✅ | 实测形状：GET /api/approvals（PENDING 列表）、GET /:id（binding/approved_by/attempt_no；**expires_at 未携带——缺口记录**）、POST 创建 201/200、approve/reject 200={ok:true,status,reason:NOOP 幂等}/409={ok:false,reason:EXPIRED\|INVALID_TRANSITION}、无主体 422 principal_required、生产模式 POST 405 |
| console-pg 审批适配 | ✅ | sources.js listApprovals/getApproval/decideApproval（dev-only 标记；决策路由 /approve\|/reject；X-Test-Principal 仅 test-auth 模式）；与内存 fixture 演练彻底分离 |
| ApprovalsPage 双模式 | ✅ | console-pg 源 → 真实 test-auth 票据列表/详情/批准/拒绝；其他源 → 未接入 + 合成票据演练（原样保留） |
| 浏览器九路径 | ✅ | 列表（HTTP→PG）→详情字段（repo/head/run/finding/action+TTL 缺口如实）→批准 T6 200 APPROVED→刷新后 GET 详情仍 APPROVED（持久化一致）→提交中按钮禁用（防重复）→已 APPROVED 重复决策 200 NOOP（不重复生效）→过期票 409 EXPIRED"需后端重签，控制台不续期"→无主体 422→生产模式 401/405 |
| head 冲突语义 | ✅ 差异记录 | 后端决策接口无 expected_head 参数（head 校验属使用/合并阶段 D-3）——UI 展示绑定 head 供核对，"head 冲突"错误语义暂无对应端点行为 |
| 缺陷修复 | ✅ | harness 反代透传 POST 方法/请求体/X-Test-Principal（此前 GET 降级 + 头丢失导致决策 422/404）；decideApproval 决策枚举误拼路由（/APPROVED→/approve）修复 |
| 测试 | ✅ 64/64 | 全套回归通过；未新增长期规避逻辑 |

**仍未接通（如实）**：真实 OAuth 登录（D-9）、正式审批授权策略（D-1/D-2/D-3 决策仍待拍板——
当前 test-auth 主体绕过 D-2 检查，仅限隔离环境）、契约 v2 /api/pulls 聚合与 current_head 权威（C-10）、
站内合并（C-12 关闭）。**PG HTTP 联调与 test-auth 审批联调已完成；真实 PR 闭环仍未达成。**

---

## 第七轮：后端缺陷修复确认 + PG 联调 10/10（2026-09-23）

后端 HEAD 推进至 `e82bcfa`（51e1a89 + 18586cc + 973fb80 + 903e995 + b8dae41 + e82bcfa）。
上轮记录的 console_pg 缺陷①②已在最新代码修复并实测确认；缺陷③（autocommit/idle-in-transaction）
未改，保留为协调项（前端联调用独立库规避）。

| 工作项 | 状态 | 说明 |
|---|---|---|
| 缺陷① runs_for_pr repo_id | ✅ 已修复 | SELECT/keys 补 repo_id（代码核实）；`GET /api/runs?repo&pr` 实测 200 |
| 缺陷② StorageUnavailable NameError | ✅ 已修复 | 改 `except Exception` + isinstance(RuntimeError) → 503；不可达 DSN 实例实测返回 `503 backend_unavailable` JSON |
| 缺陷③ idle-in-transaction | ⚠️ 保留 | autocommit=False 未改——仅与后端测试窗口并发 DDL 时互塞；前端联调已用独立库规避 |
| 10/10 浏览器复验 | ✅ | 仓库列表/PR 列表/PR 详情/多 head（PR#9 a2+a1）/多 run（RUNNING+SUCCEEDED）/stages+events+evidence（MinIO 未接线如实）/跨仓库同编号 #9 不串/分页 offset 稳定/404 不回退快照/503 backend_unavailable——全过 |
| 文案诚实性 | ✅ | ReposPage 在 console-pg 模式不再误标"当前数据模式 snapshot"，改"隔离 PG 只读服务：fixture 测试记录"+ "PG Fixture 测试记录" chips |
| 证据 | ✅ | verification/console-pg-itest/：rerun-10of10.log + 截图 05~08（修复后复验）+ 首验 01~04 |

**结论**：隔离 PG 只读 HTTP 联调 **10/10 通过**（后端 HEAD e82bcfa，数据 data_mode=fixture，
非真实 PR 审查完成）。仍未接通：契约 v2 /api/pulls 聚合与 current_head 权威（C-10）、
verdict/待办字段、真实认证（D-9）、审批读写（D-1~D-3，本轮未启用 test-auth）、站内合并（关闭）。

---

## 第六轮：console_pg 隔离 PG 只读联调（2026-09-23）

后端交付 51e1a89 + 18586cc（tools/console_pg/server.py v0.2.0：只读 HTTP；/api/repos、/api/prs、
/api/runs、/api/runs/:id、/api/auth/session=401、写 405；data_mode 恒 fixture）。本轮完成
**隔离 PostgreSQL 只读 HTTP 联调**——数据为隔离 PG fixture 测试记录，**不称真实 PR 闭环**。

| 工作项 | 状态 | 说明 |
|---|---|---|
| 接口核实（可复现请求） | ✅ | console_pg @ :4193（`dev/console-pg-launch.py` 启动，独立库 mp_pg_console_fe，4 条确定性 fixture run 经后端 PgRunStore API 写入）；不再沿用"PG 无 HTTP"旧结论 |
| 形状对比 | ✅ 记录 | console_pg 为 console-0.1.0 形状（/api/prs），非契约 v2 /api/pulls 聚合；无 current_head 权威/verdict/has_pending_tickets——前端**如实降级**为最近记录口径，不显示当前结论、不从历史 HIGH 生成待办；差异记录于 C-10 备注 + verification/console-pg-itest/README.md |
| console-pg 适配层 | ✅ dev-only | sources.js consolePgSource（明确标记"DEV/隔离联调适配，非正式契约"）：/api/repos、/api/prs、/api/runs（规避后端 /api/runs?repo&pr 缺陷：repo 级查询+客户端过滤）、/api/runs/:id（stages/events/evidence/merge_panel 懒加载） |
| harness PG 模式 | ✅ | `contract-fixture-harness.mjs --pg <base>`：/api/health 声明 console-pg 源 + /pg/* 透传代理（GET 查询 + test-auth 审批决策 POST——写仅限隔离 fixture 票据，由隔离后端强制校验；同源解决无 CORS）+ session 透传（401 如实）；NODE_ENV=production 拒启 |
| 浏览器联调十项 | ✅ 9/10 +1 记录 | ①仓库列表 ②PR 列表 ③PR 详情 ④多 head ⑤多 run ⑥stages/events/evidence ⑦跨仓库同编号 #9 不串 ⑧分页稳定 ⑨不存在 run 404（不回退快照）——全过；⑩PG 不可达 503：**后端缺陷致无法返回**（见下），语义已按契约在前端预留 |
| 认证/隔离专项 | ✅ | session 401 如实 → "PG Fixture · 未认证"常驻；live/PG 失败不回退私有 snapshot（源分离测试）；模式仅由 /api/health sources 声明；浏览器无 token/私钥 |
| 测试 | ✅ 64/64 | 全套（不含门控跳过的 console_v3 集成）；snapshot 模式 4730 回归通过 |
| 证据 | ✅ | verification/console-pg-itest/（README 联调记录 + 4 截图）；contract 模式 harness-01~04 保留 |

**后端需修正（console_pg @ 18586cc，精确复现见 verification/console-pg-itest/README.md）**：
① `/api/runs?repo&pr` 路径 `_run_record` KeyError（runs_for_pr 行缺 repo_id）→ 连接重置；
② `except StorageUnavailable` 未定义 → PG 不可达时 NameError 替代 503 语义；
③ 协调项：只读连接空闲持事务会阻塞其他窗口的 schema drop（建议只读 autocommit）。

**仍未接通（如实）**：契约 v2 /api/pulls 正式实现、current_head 权威、verdict/待办字段、
真实认证（D-9）、真实审批读写、站内合并。**PG HTTP 联调已完成；真实 PR 闭环仍未达成。**

---

## 第五轮：页面级接入准备 + 契约 fixture 页面验收（2026-09-23）

后端仍未交付 PG 只读 HTTP（HEAD 4a686a8 复核：仅 console_v3 只读 /api/runs；PG 为存储层+读模型函数）。
本轮完成"页面级接入准备"并以 dev-only fixture harness 完成**正式契约 HTTP fixture 页面验收**（非 PG 接通、非真实运行）。

| 工作项 | 状态 | 说明 |
|---|---|---|
| 适配层接入盘点 | ✅ | 上轮 api-live 仅 fetchSession 进页面路径，其余仅测试调用；本轮补齐页面级接入 |
| 数据源注入机制 | ✅ | `src/data/`（config/pr-view/sources）：模式由提供服务的后端经 /api/health `sources` 声明（console 后端已扩展该声明）；sessionStorage/URL 无权授予 contract/live；声明缺失保守回退 snapshot |
| 页面双来源 | ✅ | ReposPage/RepoPrsPage/PrDetailPage 经 `useDataSource` 消费统一数据源：snapshot（原路径不变）/ contract（/api/pulls 契约形状 → PrView 映射）；运行历史导航按能力呈现（契约源隐藏 + 路由如实说明） |
| 契约语义 | ✅ | contract 视图：current_head_sha=权威；latest_result.stale→"旧 head 结论"标注；当前 head RUNNING 无完成结果→明确"进行中，不用旧结果顶替"；has_pending_tickets=后端权威真实待办；历史 HIGH 只标"历史记录中需关注"不生成当前待办 |
| fixture harness | ✅ | `dev/contract-fixture-harness.mjs`（NODE_ENV=production 拒启；无存储/授权/审批状态机；合成数据）：静态 dist + /api/health + session/capabilities/pulls/pulls/:n + 合成补丁文件 |
| 浏览器页面验收 | ✅ | harness :4192 实测：仓库→PR 列表→详情→历史→返回；跨仓库同编号 PR #9 不串数据（widget/other 深链接互证）；stale/进行中/权威 head 场景正确；补丁下载入口与"下载≠已应用"、缺失项如实显示不制造空链接；fixture 标识常驻（模式 chip + 会话 chip + 详情 chip）；截图 4 张 `verification/console-contract-alignment/` |
| snapshot 回归 | ✅ | 4730 演示预览：仓库/待处理/运行历史导航与数据照常（双模式互不干扰） |
| 测试 | ✅ 65 项 | 新增 data-layer 8 项（配置回退/源分离互不调用/失败不回退不冒充空数据/竞态守卫/契约视图映射/pending_tickets 权威）；56+8 passed + 1 门控跳过 |
| 本轮修复 | ✅ | harness 数据构造缺陷：current_head_sha 曾由 latest_result 派生（违背契约权威语义）——改为显式声明并核对前端展示与接口一致 |

**仍未接通（如实）**：与第四轮一致——真实登录（后端+D-9）、/api/pulls 正式实现（PG HTTP）、真实审批、站内合并。
本轮结论 = **页面级契约验收完成，PG 联调待接口**。

---

## 第四轮：契约对齐 + 接线准备（2026-09-23）

后端尚未完成真实 PR 闭环的 HTTP 层（FRONTEND-HANDOFF：PG 仅存储层+读模型函数，**尚无正式 HTTP 接线**）。
本轮为"契约对齐与接线准备"：按已接受契约 7ccecb9（接受记录 c664df2）对齐，不宣称真实闭环完成。

| 工作项 | 状态 | 说明 |
|---|---|---|
| 后端实际接口核实 | ✅ | 真实 HTTP 仅 console_v3 只读 `/api/runs`/`/healthz`（SQLite，数据自标 shadow/fixture）；契约 v2 端点（auth/session、capabilities、pulls、approvals、merge）**全部未实现**；PG 无 HTTP |
| 会话端点对齐 | ✅ | `GET /api/auth/session`（原 `/api/session` 偏差已修正）；401(not_authenticated/session_expired)/403(not_a_member)/503(auth_unavailable)/404(未实现) 分类矩阵；~~"认证方案待拍板"~~ 已删——契约已定（GitHub OAuth+服务端会话），等待后端实现 + D-9 配置 |
| 契约适配层 | ✅ | 新增 `api-live.js`：session/capabilities/pulls/approvals 端点形状、`?repo=` 寻址编码、CSRF（全部副作用方法含 PATCH，缺 token 本地拦截）、错误 envelope reason 透传；**不含后端业务逻辑** |
| head 权威逻辑 | ✅ | `pr-model.associateCurrentHead()`：有权威 head 时结论按 head 匹配才归当前；当前 head 进行中→"无完成结果"明确说明；旧 head 全标 stale（不用最近成功掩盖当前失败）；无权威（snapshot 现状）维持"最近记录" |
| fixture/模式分离 | ✅ | pulls 契约 fixture（data_mode:"fixture"，含 stale+running 场景）；审批 fixture 适配器与只读适配器（approvalsUrl 等）彻底分离；真实审批/合并保持关闭 |
| 契约测试 | ✅ 56/56 | 新增 live-api 8 项（路径/分类矩阵/CSRF/fixture 形状/head 关联）+ pr-model 关联 3 项；集成测试 live-v3（环境门控，MERGEPILOT_V3_URL 未设置自动跳过） |
| 隔离联调 | ✅ 真实 HTTP | console_v3 @ :4191（隔离 fixture 库）：列表→适配聚合→详情→404→405 全链路通过；数据保留 shadow/fixture 标签、不标为真实运行；证据 `verification/console-contract-alignment/v3-itest.log` |
| 验收样例 | ✅ | `CONTRACT-ACCEPTANCE-SAMPLES.md`：后端可复现的请求/响应样例（session/capabilities/pulls/pulls/:n/merge 403/已联调 console_v3） |
| 文案对齐 | ✅ | 登录页/设置页"认证方案待拍板"→"契约已定（7ccecb9）等待实现+D-9"；待处理页改"历史记录中需关注"（历史 HIGH 不生成当前待办，真实待办待 C-4/C-11 票据） |
| 浏览器验收 | ✅ 状态矩阵 | 会话 404→登录页（新文案）→演示预览→各页正常；不重复上轮全量视觉测试 |

**仍未接通（如实）**：真实登录（等后端+D-9）、真实 PR 列表/详情（等 /api/pulls 实现）、
PG 运行记录展示（等正式 HTTP 只读 API——"适配与契约完成，接线未验证"）、真实审批（等票据 HTTP+D-1~D-3）、
站内合并（契约默认关闭）。

---

## 第三轮：仓库/PR 中心工作台（2026-09-22）

用户要求把控制台从"历史 run 平铺"改为"以仓库和 PR 为中心"，并为登录、人工审批、未来合并建立
交互与接口边界。分批 A/B/C 执行，全部浏览器验收；**未接通的能力一律如实标注，不用模拟成功冒充**。

| 工作项 | 状态 | 说明 |
|---|---|---|
| 能力核实 | ✅ 记录在案 | 数据源=snapshot API；无登录（/api/session=404）、无审批端点、无 PR 当前 head 权威、仓库列表=快照推导——全部在页面如实标注 |
| 批次 A：信息架构 | ✅ | 默认入口 `/repos` 仓库工作台（"历史数据中的仓库"标注，PR/run 分口径统计）→ `/repos/:owner/:name` PR 聚合列表（一 PR 一行、搜索+需要处理筛选+分页 10/页、URL 承载筛选、返回保留滚动）→ `/repos/:owner/:name/pr/:n` PR 详情（摘要标注基于最近/最近完成记录→问题→待处理→阶段状态→历史运行按 head 分组折叠→技术详情）→ `/runs` 降为运行历史 |
| head 诚实边界 | ✅ | snapshot 无当前 head 权威：聚合体不产出 currentHead/currentVerdict；PR 列表与详情均注明"基于最近一次运行记录，不代表当前 head（C-10）"；同 head 组内只标组内最近一条结论 |
| 批次 B：会话 | ✅ | AuthProvider 五态状态机 + 只读演示预览（sessionStorage、顶栏常驻"只读演示预览·未认证"）；登录页无假表单；服务不可达与未登录分开呈现；刷新保留演示态（实测） |
| 批次 B：审批 | ✅ fixture | `/approvals` 默认"审批服务未接入"；测试数据模式（合成票据 FIXTURE-*，内存演练零写操作）验证四路径：正常批准 / head 冲突预检拦截 / 过期预检拦截 / 超时先查询后确认；预检不过禁用按钮、提交中防重复点击、不做乐观更新 |
| 批次 C：接口交接 | ✅ | INTEGRATION-REQUESTS 新增 C-8 会话 / C-9 仓库权限 / C-10 PR 分页+当前 head 权威 / C-11 审批读写（更新 C-4 边界）/ C-12 站内合并（范围变更记录，未实现）/ C-13 能力标识 |
| 批次 C：合并边界 | ✅ | PR 顶部 GitHub 查看入口；审批与合并明确是两件事；无假合并按钮；不调用 GitHub API、不申请权限 |
| 视觉 | ✅ | 保留品牌（冷灰+墨色+深青）；仓库名升为标题；PR 为主对象；"需要处理"仅单点标注不铺红；删长篇工程说明（集中到设置页）；去表格嵌套滚动 |
| 响应式/键盘 | ✅ | 1440/1920/390 实测：小屏隐藏次要列、顶栏让位、按钮 nowrap；键盘 Enter 激活演示预览入口、全局焦点环、徽章可聚焦 |
| 测试 | ✅ 48/48 | 新增 pr-model 9 项（聚合/跨仓库同编号不合并/旧 head 不冒充/分口径计数/分页）+ approvals-model 5 项（预检/防重/超时查询/终态/不乐观更新）；node --test 全绿 |
| 浏览器验收 | ✅ | 登录守卫→演示预览→仓库→PR 列表→PR 详情→历史分组→待处理→审批全流程截图存 `verification/console-pr-workbench/`（11 张，含改版前对照） |

**本轮抓到并修复的真实 bug**：① ApprovalsPage 缺失 `useAuth` import → 渲染 ReferenceError 整树白屏
（浏览器验收抓到；顺手加 ErrorBoundary 单页兜底）；② PR 标题只看最近一条记录，旧包有标题最近包没有时
退化为 PR #n（改为同 PR 任一记录取最近非空标题）；③ 390px 下标题列竖排挤压、退出按钮竖排。

**边界与未接通清单（如实）**：无登录/无审批/无合并后端——登录只有交互结构；审批只有 fixture 演练；
合并只有 GitHub 外链与需求交接；仓库列表是快照推导；PR 摘要全部是"最近记录"口径。

---

## 第二轮：前端重构"运行取证台"（2026-09-22）

用户反馈"页面过于抽象"，要求现代管理系统设计美学。按 impeccable new-work 流程执行**表面级视觉世界替换**（brief-pinned，Operate 模式）：

| 工作项 | 状态 | 说明 |
|---|---|---|
| 方向契约 | ✅ | index.html `impeccable:direction` 注释（THESIS/OWN-WORLD/STORY/FIRST VIEWPORT/FORM/FINISH），构建产物已验证存活 |
| 视觉世界替换 | ✅ | 冷灰阶 + 墨色侧边栏 236px + 深青 #0e6b62 唯一强调 + 四状态色仅状态；lucide-react 图标系统（新增唯一前端依赖）；tabular-nums；浏览器面接管（selection/焦点环/滚动条/caret） |
| 组件重构 | ✅ | 分区图标导航、SNAPSHOT 模式徽章（走针时钟）、计数快筛 chips、骨架屏、文件类型图标、徽章"点+文字"、证据抽屉 220ms 滑入 + 焦点移入 |
| 产品真相保留 | ✅ | 三态分离/诚实空值/只读边界/证据转义全部不变；后端仅新增 review.human_gate_source 字段 |
| 截图检查 | ✅ 两轮 | 桌面 1280 + 窄屏 768；修复批次：表格列断行/宽度压缩（发布状态列落回 1280 首屏+滚动提示）、计数 chip 空串 bug、门徽章语义标签 |
| Finish review | ✅（替代评审，披露） | 新鲜上下文子代理按 craft-floor 评审：裁决 fix → 修复批次（M1 SHA 断行、M2 首屏三态、7 minors）→ 复评确认见 .impeccable/review/ |
| 设计记录 | ✅ | console/DESIGN.md（表面级，从建成世界提取；根 DESIGN.md 属 E2E 演示控制台不约束本表面）+ surface brief |

**决策记录**：①重设计而非打磨（用户 brief 钉死新美学，旧"交接班看板"世界作 anti-reference）；②lucide-react 为唯一新增依赖（构建期，随 bundle 打包，无运行时外部请求）；③根 DESIGN.md 不改写（属另一表面的既有记录），以 console/DESIGN.md + 文件级 ignore 豁免（.impeccable/config.json，理由注明）；④"全部"计数 chip 空串 `??` 不触发是本轮抓到的真实 JS bug。

---

## 第一轮：运行查询闭环（snapshot 模式）

| 工作项 | 状态 | 说明 |
|---|---|---|
| 运行列表 | ✅ 完成 | 18 个真实历史运行包；仓库/PR/head SHA/run_id + **执行状态/审查结论/门/发布状态四列分离**；筛选（repo/PR/三态/搜索）；诚实"未记录" |
| 运行详情 | ✅ 完成 | 概览（身份+三态卡+归属一致性声明）/ 时间线（ledger+kickoff+任务+门+check-run 合并排序）/ 任务表 / 证据 / 版本清单 / Skill / RAG / 用量 8 标签 |
| 证据查看与下载 | ✅ 完成 | 纯文本转义渲染（无 HTML 执行）、512KB 截断标注、SUMS 已列/未列标注、下载附件化；路径穿越防护有测试 |
| 包完整性校验 | ✅ 完成 | 按需 SHA256SUMS 全量校验（浏览器实测 SK5 PR2：54/54 通过，1 项未列入） |
| RAG 状态区分 | ✅ 完成 | 已调用（含逐条 source_refs/命中数/SYNTHETIC 语料警示）/未调用/仅计数/数据不足四态；会话累计口径如实注明 |
| 用量 | ✅ 完成 | 实测窗口（Higress 网关口径）+ note 原文；金额不显示（无价目表，不虚构） |
| 前端设计 | ✅ 完成 | 遵循仓库 DESIGN.md（交接班看板体系：暖灰地/墨黑导航/状态三色/焦点深青）；浏览器截图视觉检查通过 |
| 测试 | ✅ 24/24 | 夹具单测 + 真实证据根回归（身份提取/结论绑定 commit/拒绝案例回退） |
| 未接入页面 | ✅ 如实标注 | 仓库/RAG 总览/Skill 总览/审批/用量 5 个导航为 StubPage，注明依赖与阻塞原因 |

## 技术决策（已记录）

1. **位置 `console/` 新目录**：不混入 demo-platform（那是比赛回放演示，Filmstrip 状态机与产品控制台耦合会互相污染）；复用其**技术栈选择**（零依赖 Node 后端 + React/Vite 前端）与部分模式（SHA256 校验、诚实标注），代码独立。
2. **后端零依赖 Node**（demo-platform backend 同款）：无新依赖、可 `node console/backend/server.mjs` 一键启动；预构建 dist 已提交，运行不需 npm。
3. **run 索引规则**：`evidence/` 下含锚点文件（delivery-ledger.json / kickoff.json / project/meta.json / PR-METADATA.md）之一的目录才索引为 run（当前 18 个）；实验包（DUAL-REVIEWER-EXP）与 PHASE14 旧包不冒充 run。
4. **身份提取优先级**：repo/PR/SHA 依 ledger > result.md > PR-METADATA > 门决策文件 > check-run URL；拒绝案例（无 result.md）回退 reviewer 任务 result.md。逐字段记录于 API-CONTRACT。
5. **端口 4730**：避开 demo 4173 与另一会话服务。

## 验证记录（2026-09-22 实测）

- `node --test`（3 文件）→ **24 passed**（真实证据根回归在内，证据目录缺失的机器自动 skip）
- 浏览器实测（ZCode IAB）：列表 18 行渲染、筛选；SK5 PR2 详情 8 标签；证据抽屉打开/关闭/Esc；完整性按钮 54/54；RAG 表 SYNTHETIC 警示；截图视觉检查通过（布局无破损）
- 修复过程抓到并修掉一个真实前端 bug：证据标签派生态竞态导致 React 卸载（改为同步 useMemo）

## 未验证 / 未做（如实）

- **live 模式未接**（需 R 系列授权：服务器 PG 只读、MinIO meta 清单读取——见 INTEGRATION-REQUESTS）
- 小屏（<900px）只做了 CSS 降级，未逐一浏览器实测
- 键盘遍历/读屏仅做了语义化（role/tablist/aria-selected）与焦点环，未做专门无障碍审计

## 下一步（按序）

1. run-manifest 接入：新 run 产生后从 MinIO 读 `run-manifest.json`（需 R-2 只读授权）→ 版本清单页从"未记录"变实证；
2. 审批只读门页：P-1 拍板后按 M2-GATE-STORAGE-OPTIONS 实现 MinioTicketStore 只读展示（不接真实执行）；
3. live 运行列表：授权后加 `delivery-ledger` 实时查询适配层（数据模式标注 live，与 snapshot 并存不冒充）。
