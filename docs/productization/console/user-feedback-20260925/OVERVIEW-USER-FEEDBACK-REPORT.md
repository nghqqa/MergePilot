# OVERVIEW USER FEEDBACK REPORT — 运营总览内部用户体验验收（2026-09-25）

**判定：`OVERVIEW_USER_FEEDBACK_BLOCKED`**

P0×1（FB-01 待处理页越权仓库数据）；核心任务中「认证与仓库边界无误导」未达成。
本轮为纯验收：**零代码修改、零后端合同变更、零安全边界变更**（git diff 证明见 §9）。

---

## 0. 环境与口径

| 项 | 值 |
|---|---|
| 被测 | canonical console `/overview`，分支 feat/admin-console，HEAD `a079367`（含基线 `d12c2d8`，未回退） |
| 栈 | mp-cc-console:candidate @ 127.0.0.1:48190 + mp-cc-pg(:45434) + mp-cc-minio —— **隔离 staging**，代码与 worktree 逐字节一致（core-pilot.mjs md5 双验） |
| 数据 | overview/pending 核心 API = **POSTGRESQL_LIVE**（staging PG）；legacy 页面 = 本地快照证据包（页面各自如实/失真标注见 FB-02） |
| 操作员 | pilot（唯一已批准操作员，环境变量注入凭据） |
| 边界执行 | 无 GitHub 写入 / 无 approve·reject·push·merge / 无 Fixer·Verifier / RAG registration=DISABLED / embedding=DISABLED / RUN_BINDING_AUTH=NOT_WIRED / 未触共享 PG·MinIO / 未新增用户·仓库·PR·权限 |

## 1. 认证体验（§一）

| 检查 | 结果 |
|---|---|
| 未登录 API | `/api/overview`、`/api/pulls` → **401** + `{reason:not_authenticated}` ✓ |
| 未登录页面 | 跳转登录页（截图 shots-1440/overview-unauthenticated-1440x900.png）；数据范围说明清楚；**无受保护数据** ✓ |
| 登录流程 | 具名操作员+密码，错误凭据 → **「凭据无效」**清晰提示 ✓；正确凭据登录成功返回 /overview ✓ |
| 退出 | **UI 无退出入口**（FB-06）；后端 `/api/auth/logout` 契约存在但无处可点；登录页文案「设置页可退出」与实际不符 |
| 退出后 | 程序化登出（logout API 200）→ /overview 即要求登录，受保护数据不可见 ✓ |
| 旧会话 | 后端内存会话：后端重启即失效（诚实恢复语义）；浏览器遗留会话在本轮开始时仍有效（并行 user-pilot 会话所建），表现为打开 /overview 直接是已登录态——行为符合设计但首屏无任何「会话建立于何时」提示（P3 备注） |
| 会话身份 | **「已登录：未知用户」**——身份不显示（FB-06） |

## 2. 运营总览信息理解（§二）

- **10 秒任务 ✅**：KPI 行「2 个 PR · 4 个运行 · 待处理 0 · 异常：stale 0 / 失败回执 0 / 完整性冲突 2」一眼可读，数字层级清晰。
- **30 秒找 PR ✅**：`nghqqa/tizhou #2`（head c443150958b3，run-canary6-tz-2，16:12:52）、`wookat/speaktype #426`（head dc425e12f683，run-canary6-st-426，16:12:50）均在明细表前两屏内，PR 链接蓝色可辨识（但点击跳转断裂 → FB-03）。
- **状态语义 ⚠️**：PASSED→「已通过」存在"是否=已合并"误解风险（FB-14）；BLOCKED→「已阻断」、STALE→「已过期」可读；REVIEWING/REMEDIATING/VERIFYING 本部署恒 0 且页面明确说明 Fixer/Verifier 禁用不虚构——**这是本页最好的诚实设计之一**。
- **图表 ❌（桌面）**：三张图在 1440/1280 宽度下无柱体/折线（FB-04），390 下正常；趋势数据在 API 层漏计当日（FB-05）。
- **跳转 ❌**：PR 链接断裂（FB-03）；图表柱体跳转因柱体不渲染不可用；`/pending`、`/repos`、`/audit` 顶部导航本身可达。
- **返回上下文 ✅**：返回 /overview 后筛选与数据保持（30s 自动刷新时间戳更新）。

## 3. 数据可信度与状态表达（§三）

- source/stage_source **可找到**：`数据来源详情`折叠面板展开后完整显示 POSTGRESQL_LIVE、生成时间、阶段推导规则（票据/gate 审计/回执完整性/head 排序）；健康 chip「PG 实时」首屏可见（FB-13）。
- **系统性矛盾（FB-02）**：settings/diagnostics/datasources 三页均称「snapshot/只读快照/生产后端未连接」，与 overview 的 POSTGRESQL_LIVE 互相矛盾；顶栏徽标「只读快照」同病（FB-10）。
- 未发现用户会误以为 RAG/自动修复/GitHub 写入已启用——页面多处明确「不写 GitHub」「Fixer/Verifier 禁用」「MinIO 未接线」；RAG registration=DISABLED、embedding=DISABLED、RUN_BINDING_AUTH=NOT_WIRED 状态未变。
- BLOCKED 未被表达为系统故障（有 stage_source 说明）；PASSED 有"是否=已合并"歧义（FB-14）；空数据有诚实空态（但与总览矛盾 → FB-03）。
- 无凭据/路径/堆栈泄露；错误响应均为结构化 JSON。

## 4. 视觉与交互（§四，视口 1440×900 / 1280×800 / 390×844）

- **首屏结构 ✅**：标题→副题→KPI→健康 chips→三图→明细表，层级清楚；桌面首屏即全含。
- **中文与混排 ✅**：中英文混排、等宽（head sha、stage_source）均易读；无文字截断/重叠；390 无横向滚动（380=380）。
- **一致性 ✅**：卡片/表格/Tag/Alert 风格统一（antd5 体系）；健康 chips 大小一致。
- **缺陷**：图表桌面空渲染+横向滚动条（FB-04）；重复信息：顶栏「只读快照」徽标 vs 页脚「只读」vs 健康区「PG 实时」三处口径不一（FB-10）；KPI「完整性冲突 2」与明细无交叉引用（FB-08）。
- **移动端**：纵向滚动 4-5 屏可接受；汉堡菜单出现；图表正常（FB-12）。
- ** Drawer/返回**：PR 详情页有「返回 wookat/speaktype」与面包屑 ✓。
- **文案**：整体克制诚实（加分）；设置页「运行边界」6 条偏工程化（FB-15 P3）。

## 5. 键盘与无障碍（§五）

- **P1（FB-07）**：Tab 被侧栏 menu 容器吸收，12 次采样均无法进入菜单项/主内容；PR 链接 tabindex=0 永远收不到焦点；图表 region 的「键盘可用」声明不成立。
- 状态颜色均伴随文字（已通过/已阻断）✓；登录框 focus 环可见 ✓。
- 局限（自动化环境）：真实 200% 缩放、逐项触控目标量测、读屏软件未验证；已记录为验证局限而非通过。

## 6. 异常与空状态（§六）

| 状态 | 结果 |
|---|---|
| 未认证 401 | ✓（API 401 + 页面登录墙，双验证） |
| 未授权 403 | 未直接触发（无 403 场景注入手段且不改配置）；allowlist 过滤行为经 PG 对照验证 |
| MinIO 未接线 | ✓「MinIO 未接线」chip + 平台边界说明 |
| BACKEND_NOT_WIRED / BACKEND_ERROR / API 超时 | 未在本轮直接触发（需改 staging 配置，超只读约束）；core-pilot.mjs 契约与 70 项测试覆盖 fail-closed 语义——记为**验证局限**，不宣称通过 |
| 空数据/图表无数据 | 仓库页空态诚实；总览图表"无数据"实为渲染缺陷（FB-04） |
| BLOCKED/完整性冲突 | KPI 可见+行内 stage_source 可溯源；缺交叉引用（FB-08） |
| 泄露检查 | ✅ 无凭据/内部路径/堆栈外泄 |

## 7. 数据一致性（§七，API × PG × 页面）

| 检查 | 结果 |
|---|---|
| PR 数（2）、repo（2）、PR 号（#2/#426）、head SHA（c4431509…/dc425e12…） | API=PG=页面 ✓ |
| stage / stage_source | PASSED×2（skill_gate_audit PRODUCE）/ BLOCKED×2（skill_receipt_outbox.integrity）三方一致 ✓ |
| pending=0 | API=页面 ✓（allowlist 外 fixture 票据被正确排除） |
| incidents | integrity_conflicts=2 = PG canary5 两条 integrity=CONFLICT ✓ |
| 图表 vs 明细 | 桌面渲染不一致（FB-04）；trend API 漏计当日（FB-05） |
| 图表点击详情 vs 总览 | PR 详情页数据源断裂（FB-03） |
| 两仓库串数据 | 无 ✓ |
| 未授权仓库出现在总览 | 总览未出现 ✓；**/pending 出现（FB-01，P0）** |

## 8. P0–P3 问题清单（15 条，明细见 overview-user-feedback.json）

| 级别 | 编号 | 摘要 |
|---|---|---|
| **P0** | FB-01 | /pending 展示 allowlist 外仓库的 HIGH 审查发现且无快照标注 → 安全边界误解 |
| P1 | FB-02 | settings/diagnostics/datasources 数据模式(snapshot) 与 overview(PG 实时) 系统性矛盾 |
| P1 | FB-03 | PR 明细链接断裂：详情页读 snapshot 显示「无运行记录」 |
| P1 | FB-04 | 桌面宽度三图表无柱/线（390 正常）+ 图内横向滚动 |
| P1 | FB-05 | 趋势 API 漏计当日数据（09-25 有 4 run 显 0） |
| P1 | FB-06 | 无退出 UI；会话身份「未知用户」；登录页文案与实际不符 |
| P1 | FB-07 | Tab 焦点被侧栏 menu 吸收，主内容键盘不可达 |
| P2 | FB-08 | 表格无 PR 当前阶段聚合；冲突 KPI 无交叉引用 |
| P2 | FB-09 | 「审计入口…见 。」空链接文字 |
| P2 | FB-10 | 顶栏「只读快照」徽标失真 |
| P2 | FB-11 | 登出后立即登录遇 CSRF 失效（刷新才可登录） |
| P2 | FB-14 | 「已通过」有"已合并/已发布"误解风险；阶段定义无帮助文本 |
| P3 | FB-12 | 移动端需 4-5 屏纵向滚动（可接受，图表反而正常） |
| P3 | FB-13 | 数据来源详情默认折叠；加载瞬态 source 空白 |
| P3 | FB-15 | 品牌「V0」与页标题不统一；运行边界段落偏工程化 |

## 9. 未修改代码证明与回归

- `git status`/`git diff --stat`（console worktree）：**空** —— 零代码修改（.mimosa 会话状态除外，未提交未还原）。
- 本轮唯一入库变更 = 本报告 + feedback JSON（新增文档，非代码）；提交于 feat/admin-console 本地，未 push。
- 回归：console 后端测试 **70 passed / 0 failed**（显式列出 9 个测试文件；目录模式调用因 shell 引号解析失败非测试失败）。staging 栈未重启、未改配置，并行 user-pilot 会话不受影响。

## 10. 结论

核心查询任务（找 PR、读 KPI、辨阶段）在数据层全部可完成且三方一致；但 **FB-01 构成安全边界误解（P0）**，按验收标准输出：

```
OVERVIEW_USER_FEEDBACK_BLOCKED
RAG=EXCLUDED
embedding=DISABLED
RUN_BINDING_AUTH=NOT_WIRED
GitHub writes=DISABLED
Fixer/Verifier=DISABLED
```

修复顺序建议：FB-01（边界过滤）→ FB-02/FB-10（数据模式口径统一）→ FB-03/FB-05（live 数据层补齐+趋势分桶）→ FB-04/FB-07（图表渲染+键盘）→ 其余 P2/P3。所有建议仅为记录，未据以改动任何代码。
