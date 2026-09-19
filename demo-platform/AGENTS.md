# MergePilot Demo Platform

## 构建与部署

- `npm run build` —— 仅前端构建（dev 模式），**不要**用它接 `deploy:pages`：产物缺少烘焙 API，部署后生产站所有 `/api/**` 数据请求会 404/回退到 index.html。
- `npm run deploy:cf` —— **生产部署唯一正确入口**：`build:static`（`--mode static`，开启 `VITE_STATIC_API=1`）→ `scripts/bake-api.mjs`（后端 167 端点烘焙为 `frontend/dist/api/**.json`，约 165 个）→ `deploy:pages`（上传 ~194 文件到 Cloudflare Pages 项目 `mergepilot`）。注意：本地验证用 `npm run build` 会**清空 dist 里的烘焙 api/**——`deploy:pages` 已有护栏会拒绝残包，但部署前务必走 `deploy:cf` 全链。
- 部署凭证：环境变量 `CLOUDFLARE_API_EMAIL` + `CLOUDFLARE_API_KEY`（Global API Key）+ `CLOUDFLARE_ACCOUNT_ID`，仅注入当前 shell，不入库。
- 生产验证：JSON 端点须返回 `Content-Type: application/json`；`text/html` 即为 SPA fallback（文件未烘焙/路径错误）。

## 静态 API 约定

- `frontend/src/api.js`：static 模式下 GET → `<path>.json`，带查询串 → `<path>__<sorted k=v>.json`（如 `dag?at=3` → `dag__at=3.json`、`evidence?id=x` → `evidence__id=x.json`）。
- POST 仅人工门批准有客户端回放仿真（内存态，无运行时写入）；其余 POST 返回 `STATIC_DEPLOY`。

## 关键路由

- 演示页：`/demo/<caseId>`；案例 ID 形如 `fastapi-pr2-rag-traced-20260917`、`fastapi-pr3-reject`（注意：**不是** `/api/cases/` 下的 `pr2-high-risk-human-gate`，那是底层案例库 ID）。
- 舞台投影模式：`/demo/<caseId>?stage=1&step=<n>`。
- 本地联调：`npm run demo`（构建+后端，后端自动选端口）；或直接 `node backend/server.mjs` 托管 `frontend/dist` + live API。

## 演示语义红线

- 人工门是唯一人类位；批准才揭示下游已记录证据，拒绝路径下游车站渲染为 dead/未派发，**不得伪造**未记录分支的执行产物。
- 反事实分支只显示"无记录"面板并链接到真实记录该分支的兄弟案例（`gate_alt`）。
- 侧栏 roster 须按 effective gate 状态渲染，不能在用户决策前泄露已记录结局。

## 演示页设计语言（评审反馈沉淀，违例即返工）

1. **决策前零剧透**：`effGate` 只由门上的点击产生（`gateChoice`），导航/深链到门后步骤一律显示幽灵态；待决时不得出现已记录结局的任何痕迹——徽章、判定、roster、横幅、关键指标、证据预览、h1 案例名（用 `name_pending` 中性名）、分支名（含 `human-reject`/`human-gate` 字样的待决隐藏）。结局只标"例外"：`notable` 级别（POST_RUN_DESIGN/HISTORICAL_REPLAY/SYNTHETIC/NOT_EXECUTED 等非运行产物）才贴标签，真实运行产物是默认前提、不贴标。
2. **只写产品事实**：每个元素必须是系统行为（做什么/约束/产出给谁），不写面向评委的论证修辞——禁"防止X/刻意Y/不替人担责/不是省略是安全语义/既当运动员"这类句式；不宣布"这段写给谁看"（禁 `◈讲给评委`、`评委要点` 字样）。
3. **无信息量的不显示**：内部英文分类码（`REAL_EXECUTED_*` 等）只留在证据抽屉（检查器视图），页面一律中文（`LevelChip code={false}`）；GitHub 行话不搬（`Expected`→待放行、`REPLAY ACTION`→演示回放）；UI 状态不当内容标题（禁"（默认折叠）"后缀）。
4. **诚实但克制**：NOT_EXECUTED/披露信息必须存在且可查，但默认折叠、不糊屏（`NotExecutedNote` 折叠条）；档案面（`/cases` 案例库、`/evidence` 证据库、审计追踪）如实显示已记录结局——这是刻意的，不算泄露。**但工作台队列是演示入口、永远是"观众未决"语境**（`gateChoice` 只在演示页内存、不持久化），有门案例一律渲染待决中性态：`has_gate`（summarize 输出）为真时用 `name_pending`/`pr_pending`（遮分支名）、通用流水线 chip（`title_pending`）、琥珀"待人工决策"终态 chip + "人工决策·待决"徽章，不显示 REJECTED/PASS/已阻断/已验证等结局词；录局真相只出现在演示页决策后和档案面。
5. **证据区形式**：证据行 = 标题（是什么）+ 例外标签（非默认才出现）+ 查看证据；首证据块必须选"该站主题且不含结局"的真实工件（如 kickoff-as-sent.txt），勿拿运行档案 README 当首块（README 含终态字样）。
6. **折叠块标题名实相符**：`detail.title` 必须概括 `quote` 的实际内容（环境披露/检索方式/归档内容/与X对照），不准一律叫"执行细节"；新增 detail 块先自查标题是否描述内容。
7. **观览位与运行位分开标记**：loopmap 的 `viewing` 环 = 用户正在看哪站；令牌位置/门脉冲 = 运行停在哪。站名 sub 用状态词（等待决策/未到达/未派发），禁用"你在这里"这类歧义措辞（观看位置≠运行位置）。自定义交接文案（`step.hand`）是"实际交接了什么"的记录——未到达站无交接，ghost 态不渲染（否则会泄露结局词如"零派发"）。词汇全站统一：未到达（ghost）/未派发（dead）/等待决策（gate pending）。
8. **postgate 工件封印**：证据项含已记录决策或其后果的，在 `demo_cases.mjs` 标 `postgate: true`。待决时：站内联预览不渲染该工件、证据行标"决策后可见"、抽屉只给标题/级别/追溯/SHA256+封存说明（`EvidenceDrawer gated` 属性，仅 GuidedDemoPage/StageView 传；/evidence 编目页不传——档案面如实）。抽屉元数据同样要封：`meta.pr` 只显"PR #n"（分支名待决隐藏）、`meta.source_dir` 只显第一目录（历史 REJECT 目录名隐藏）、诚实性标注整块待决隐藏（HONESTY 文案本身含结局）。注意 `?ev=` 深链与复制按钮同样过封印。
9. **无门案例不许显示待决文案**：`gateIdx < 0` 的案例（rag-loop/rework/cwe22 类）一律当"已按记录完成"渲染——loopmap 状态条显示闭环完成、roster 显示已执行、侧栏门块显示记录的批准态；一切 `effGate !== 'recorded'` 分支都必须套 `gateIdx >= 0 &&` 守卫（待决文案只对有门案例有意义）。
10. **内容级指纹脱敏（第二层防线）**：postgate 封印是"按证据项"粒度的，挡不住**门前工件正文内**的结局指纹（如 kickoff 里的 `elemiso-pr3v3-reject` 项目名、PR-METADATA 里的 `demo/high-risk-human-gate` 分支名、证据目录名 `rejectDemo`/`HIGH-RISK-REJECT`）。每个门控案例在数据层声明 `pending_redactions: [[指纹原文, 替换文本]]`，`redactPending()`（bits.jsx）在**所有**待决渲染路径应用：站内联预览、StageView hero、抽屉字段/原文/标题/来源/复制全文。独立数据结构同样要管：`s.rag.records` 逐行标 `postgate`（门后角色的调用记录行），`s.rag.note` 提供 `note_pending` 待决版（数量拆分"PR2 6+PR3 2"本身即泄露）。**步骤级字段同理**：凡聚合全运行记录的门前列（`role`/`time`/`stat`/`points`/`exhibit`/`hand` 等）若含门后角色行或计数，在数据层提供 `xxx_pending` 待决变体——`pendingStep()`（bits.jsx）在 GuidedDemoPage 与 StageView 统一应用（例：rag 站 `skill ×12`→`×7`、角色分解表只留 reviewer 行）。决策点击后脱敏自动解除、完整原文恢复——脱敏是"暂缓展示"，不是篡改工件。新增门控案例时：先全量扫描门前工件的正文与步骤字段，把含结局语义的字符串列进 `pending_redactions` 或改写 `xxx_pending`。
11. **烘焙 API 文件名只许 ASCII**：带查询串的端点烘焙为 `<path>__<hash>.json`（`queryHash`：排序+解码后的规范查询 djb2→base36），`frontend/src/api.js staticApiPath` 与 `scripts/bake-api.mjs fileFor` 必须保持同一算法。禁止把 percent-encoded UTF-8 写进文件名——Pages 的 URL 解码行为会让单次编码请求静默 SPA 回退（200 text/html）。
