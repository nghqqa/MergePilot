# 控制台推进状态（console/STATUS）

**更新**：2026-09-22（第三轮：仓库/PR 中心工作台 + 登录/审批交互边界）｜ **分支**：`feat/admin-console`（worktree `D:\goai\mp-worktrees\console`，基线 `0ae8843`）
**负责目录**：`console/`（前后端）+ `docs/productization/console/`。未改动 gh-bridge / workflow-controller / approval / rag / costmeter / 共享数据结构 / r3work。

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
