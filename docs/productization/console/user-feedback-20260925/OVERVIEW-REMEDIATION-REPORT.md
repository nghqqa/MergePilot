# CANONICAL_CONSOLE OVERVIEW REMEDIATION REPORT（2026-09-25）

**判定：`CANONICAL_CONSOLE_OVERVIEW_REMEDIATION_READY`**

基线：OVERVIEW_USER_FEEDBACK 轮 86cce91（BLOCKED：P0×1 / P1×6）。
本轮在 feat/admin-console 上完成全部 P0+P1 修复并通过活体与回归验证。
分支现 HEAD = 62c32b7（并行 RAG-A 集成提交）+ 本轮提交；未 push。

## 1. 修复清单（对照反馈编号）

| 反馈 | 修复 | 实现 |
|---|---|---|
| **FB-01 (P0)** pending/repo/PR 页越权数据 | 服务端 allowlist 强制 | ①`/api/runs`+`/api/runs/:packId`+证据子资源：已认证会话按 session allowlist 服务端过滤（未知 repo 的 pack 一并 404，不泄露存在性）；匿名保留登录页明示的演示快照语义（`scope` 字段显式标注 `demo_snapshot`/`session_allowlist`）。②`primary=contract_v2`（live 时）→ pending/仓库/PR 详情页全部切换到服务端 allowlist 过滤的 `/api/pulls` 系端点，不再读未过滤 snapshot |
| FB-02 (P1) 数据模式三页矛盾 | `/api/health` 如实声明 | DSN 配置时 `data_mode='live'`、`primary='contract_v2'`、`declared_repos`=allowlist；settings/diagnostics/datasources 面板随 health 渲染（live 文案："PG 实时（staging 隔离库）…"）；无 DSN 时维持 snapshot 诚实降级 |
| FB-03 (P1) PR 钻取断裂 | 新增 `GET /api/pulls/:prNumber?repo=` | 与 `/api/overview` 同一 live 数据源 + 同一会话 allowlist：401/403（repo_not_in_allowlist）/404（no_live_record）/200（stage、stage_source、head、runs 历史、receipts 计数、gate_audit、merge_panel 关闭）；PrDetailPage 契约分支渲染控制面阶段 chip + 回执/gate 计数；404 文案显式 `no_live_record` 不回退快照 |
| FB-04 (P1) 桌面图表空渲染 | 容器尺寸修复 | 移除 plots 固定 width；`.ov-chart-box` min-width:0 + width 100%（autoFit 正确测量）；删除横向滚动容器；小屏网格 minmax(0,1fr)；表格 `scroll={{x:'max-content'}}` 容器内自滚动。验证：1440/1280/390 三视口柱/线/点全部渲染，390 零横向溢出 |
| FB-05 (P1) trend 漏计当日 | `isoDayOf` 修复 | 根因：pg 驱动返回 Date，`String(Date).slice(0,10)` 产出 "Wed Sep 25" 永不匹配 ISO 日期桶。统一 UTC 日期桶；活体验证 2026-09-25 runs=4（此前 0） |
| FB-06 (P1) 无退出/未知用户 | 会话操作补齐 | 设置页新增「退出登录」（POST /api/auth/logout + X-CSRF-Token，成功后 auth.refresh + 跳转 /overview 登录墙）；`sessionBody`/login 响应 user 结构化 `{name}`——顶栏/设置显示真实用户名；验证登出→免刷新重登闭环 ✓ |
| FB-07 (P1) Tab 陷阱 | 侧栏 `focusable={false}` | rc-menu 不再给 UL 注入 tabindex=0；Tab 序列实测：菜单 9 项链接逐个可达 → 顶栏按钮 → 数据来源详情 → 图表导航链接 → 表格 PR 链接 |

P2/P3（FB-08..15）中随修复顺带解决的：FB-09 空链接文字（Link 修复）、FB-10 顶栏徽标（live chip）、FB-11 登出后 CSRF（活体复验**未能复现**——登出后免刷新立即重登成功，原判定为测量时序假象，已如实改判）、FB-12/13 随布局修复保持。

## 2. 活体验证（一次性隔离 staging：rm-net + rm-pg(46432) + rm-console:48290）

环境：mp-cc-pg 数据的逐字节 pg_dump 副本（含两个授权 PR + 未授权 fixture
`other-org/outsider-repo`、`pilot-staging/*`），镜像 `mp-canonical-console:r4-remediation`
（digest `sha256:41bd30291aba…`）。

| 验证项 | 结果 |
|---|---|
| P0 零泄漏 | 登录会话 `/api/runs` scope=session_allowlist，allowlist 外条目 **0**；越权 pack 详情/证据子资源 404；`/api/pulls?repo=越权` → 403 repo_not_in_allowlist；未授权 fixture 票据不出现在 /pending（pending=0） |
| 两授权 PR live 一致 | overview PRs = tizhou#2(c4431509/PASSED+BLOCKED)、speaktype#426(dc425e12/PASSED+BLOCKED)；PR 详情 200 携带 stage=PASSED、head=dc425e12、receipts 4(OK3/冲突1)、gate_audit 1 |
| 401/403/404 | 未认证 401（overview+pulls/:n）；403 allowlist 外；404 no_live_record ✓ |
| trend | API trend 2026-09-25 = **4**（修复前 0）；桌面/移动图表 trend 09-25 spike 可见 |
| 图表 | 1440/1280/390 三视口柱体/折线/点全部渲染；390 零横向溢出；空数据分类仍诚实 |
| PR 链接 | React Router Link 真实 href；直连 URL 与点击均可达 |
| logout/TTL/重启 | 登出→登录墙→免刷新重登 ✓；promotion E2E P7 重启旧会话 401 + TTL 短会话 200→401 ✓ |
| 键盘 | Tab 序列实测：侧栏 9 链接 → 顶栏 → 数据来源详情 → 图表导航链接 → 表格 PR 链接 ✓ |

## 3. 回归与套件（真实退出码）

| 套件 | 结果 | EXIT |
|---|---|---|
| console 后端全量（含新增 r4-trend 3 + r4-remediation 5） | **78 passed / 0 failed** | 0 |
| promotion E2E（适配一次性栈） | **8/8 PASS** | 0 |
| user-pilot（适配一次性栈） | **4/4 PASS** | 0 |
| vite build（前端） | 成功 | 0 |
| docker build（r4-remediation 镜像） | 成功 | 0 |
| Trivy vuln+secret（新镜像） | **0 vuln / 0 secret** | 0 |
| SBOM CycloneDX | 35 组件 | 0 |
| git diff --check | 净 | 0 |

## 4. 合同变更声明（本轮有意的契约演化）

- `/api/health`：live 配置时 `data_mode='live'`、`sources.primary='contract_v2'`、新增 `declared_repos`——驱动前端统一实时源（任务 §二 的实现机制）。
- `/api/auth/*`：`user` 由字符串改为 `{name}` 结构化对象（FB-06；既有测试断言已同步更新）。
- 新增 `GET /api/pulls/:prNumber?repo=`（allowlist 强制；404=no_live_record）。
- `/api/runs`：已认证会话按 allowlist 服务端过滤；响应新增 `scope` 标注。
既有 E2E（promotion/user-pilot 原版语义）在新契约下 8/8、4/4 通过（适配副本仅改 BASE/网络/镜像名与 P2 user 形状断言，归档于 r3work/overview-feedback-20260925/e2e-adapted/）。

## 5. 事故记录（如实披露）

适配 E2E 首跑时 `docker restart mp-cc-console` 一行未替换——**误重启了并行 user-pilot 会话的 live staging 控制台容器一次**（~11:06 UTC）。影响：该容器内存会话丢失（并行窗口需重新登录）；数据零影响（只读控制台，PG/MinIO 未触碰）；容器随即自检 healthy（48190 health=200）。已修正适配脚本并复核。除此之外零外部副作用：未触共享 case-pg/MinIO、未启用 RAG/embedding/RUN_BINDING_AUTH、未启动 Fixer/Verifier、零 GitHub 写入、零 approve/reject/push/merge。

## 6. 回滚

- 镜像回滚：`mp-canonical-console:candidate`（未覆盖，digest 不变）为前一已知良好版本；rm-console 一次性容器直接销毁。
- 代码回滚：feat/admin-console 回退至 `62c32b7^`（=86cce91）即可；本轮提交为线性追加。

## 7. 工件

- 代码：feat/admin-console 本轮提交（server.mjs/core-pilot.mjs/session.mjs/store_sqlite 无关；前端 OverviewPage/PrDetailPage/SettingsPage/DataSourcesPage/WorkspaceStatusPanel/App/console.css/data/config.js + 新增测试 ×2 + 既有断言更新）。
- 截图：r3work/overview-feedback-20260925/shots-rm-{1440,1280,390}/（修复后三视口）+ PR 详情/设置/待处理。
- 扫描：trivy-r4-remediation.json、sbom-r4-remediation.cdx.json（sha256 见归档）。
- E2E 输出：promotion-e2e-output.txt（8/8）、user-pilot-output.txt（4/4）。
