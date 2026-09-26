# UI-REVIEW：审查运行页信息层级与状态语义整改（2026-09-22）

**范围**：`console/frontend` + `console/backend`（只读适配层，1 个新增字段）。
**依据**：本轮任务书（现场核查 → 修改 → 浏览器验收 → 修复）；视觉参考 = 现有控制台页面（用户浏览器停在 127.0.0.1:4730/runs）。
**不涉及**：核心编排/审批政策/预算/RAG 检索/GitHub 回写逻辑；未新增任何写操作。
**甄别说明**：主仓库 `tools/console_v3`（另一会话的 v3 编排只读控制台，Python stdlib）不是本页面对应实现；本轮对象始终是 `console/`（分支 feat/admin-console，127.0.0.1:4730）。

## 一、修改前问题（截图：.impeccable/review/ui-round2/before-*.png）

1. **满屏红绿胶囊**：每行 4 枚彩色徽章（执行/结论/门/发布），PROCESSED/COMPLETED 显示绿色（读作"通过"），check-run failure 显示红色（读作"回写失败"）——语义色被用成了装饰，与字段真实含义不符。
2. **PR 不见标题**：列表首列是仓库全名 + PR #n；证据包 PR-METADATA 里存在的真实标题未展示。
3. **"门"列术语化**：用户不易理解；且审批对象（修复？发布？）未说明。
4. **run_id 全文占列**：横向宽度被 26–32 字符的 run_id 挤压；复制操作只有 hover 才露出。
5. **顶栏跳动时钟**：秒级走针暗示实时刷新，与 snapshot 模式矛盾；"SNAPSHOT 真实历史证据·只读·非实时"工程腔且未标数据采集时间。
6. **筛选无可视 label**（仅 placeholder）、仓库为自由文本、**返回列表丢失筛选**。
7. **详情页结论先于依据**：结果摘要一行英文；findings 文件未在概览露出；span 原始 JSON 占据版本清单页。
8. **可访问性**：抽屉打开焦点不移入、关闭不恢复；text-3 在 inset 底色上对比度 4.49:1（<4.5 AA）。

## 二、状态映射（源字段 → 文案 → 颜色 → 解释）

实现：`console/frontend/src/status-map.js`（纯模块）；测试：`console/backend/test/status-map.test.mjs`（10 项，含全部语义红线）。

| 源字段 | 显示文案 | 颜色 | 解释要点（title） |
|---|---|---|---|
| execution=PROCESSED | 处理完成 | 中性 | 投递生命周期事实，不代表审查通过或检查通过 |
| execution=COMPLETED | 执行完成 | 中性 | 全部节点执行完毕，不代表无安全问题 |
| execution=BLOCKED | 已阻断 | 琥珀 | 人工拒绝/验证失败后的受控停止，PR 保持 OPEN |
| execution=ERROR | 处理出错 | 红 | 执行错误，需人工区分可恢复/人工 |
| review=CONFIRMED+CRITICAL/HIGH | 确认发现 · <级别> | 红 | 独立审查确认；发现确认≠已修复 |
| review=CONFIRMED+MEDIUM/LOW | 确认发现 · <级别> | 琥珀 | 同上 |
| review=NOT_CONFIRMED | 未发现问题 | 绿 | 明确正向；"本次审查未发现"≠绝对无风险 |
| review 缺失 | 结论未记录 | 中性 | 不等于"无问题" |
| gate=APPROVED | 已批准修复 | 蓝 | 授权事实；≠已修复、≠已合并 |
| gate=REJECTED | 已拒绝修复 | 琥珀 | 受控安全停止 |
| gate=NOT_REQUIRED | 无需人工确认 | 中性 | 低风险自动路径 |
| publish=published+success | 已回写 · 检查通过 | 绿 | 回写成功不证明补丁已验证 |
| publish=published+failure | 已回写 · 检查未通过 | 琥珀 | **两个事实**：回写通道成功 + 检查结论未通过；不是回写失败 |
| publish=processed_no_checkrun_record | 无发布记录 | 琥珀 | 台账 PROCESSED 但包内未随附 check-run；不代表未回写 |
| publish=not_recorded | 未找到发布记录 | 中性 | Matrix 轮为设计上零 GitHub 写入；webhook 轮缺失属证据不完整；不推导为"未回写" |

绿色仅出现在两处明确正向（未发现问题 / 已回写·检查通过）；红色仅出现在确认高风险与执行错误。

## 三、实际修改

**批次 A（状态/列表/顶栏/筛选）**
- 新增 `status-map.js` 映射模块（上表），status.jsx 全部改走该模块；新增 10 项语义测试。
- 列表列序改为：PR/仓库 → 审查结论 → 执行状态 → 人工确认（原"门"）→ GitHub 检查 → 开始·耗时 → 查看详情。
- PR 行：有真实标题展示标题（来自 PR-METADATA.md，仅 LIVE 两包有，其余诚实显示 PR #n）；仓库降为次级；GitHub 外链带 ↗ 图标 + aria-label + "PR 状态页 ≠ head 绑定永久证据链接"说明；短 SHA 留列表，完整 run_id/SHA 移入详情（详情保留复制）。
- 顶栏："审查运行"标题；模式徽章改"历史快照 · 只读"；新增"数据采集于 2026-09-16 ~ 2026-09-19"（由数据实际 min/max 计算）；**移除秒级走针时钟**；loopback/凭证说明移入徽章 tooltip。
- 筛选：可见 label（搜索/仓库/执行状态/审查结论）；仓库改已知仓库下拉；全部筛选进 URL query（刷新/返回保持）；"清除筛选"；计数 chips 与当前筛选范围一致。
- 后端新增字段：`pr_title`（PR-METADATA.md 标题行）、`findings_path`（review 任务 findings 明细）、`human_gate_source`（门决策来源文件）。

**批次 B（详情闭环/侧栏/焦点）**
- 概览重组为证据闭环：四态卡（各带来源行）→ **审查发现**（结论 + CWE + "依据"链接直达 reviewer 结果/findings 原文抽屉）→ 未执行/未授权 DAG 节点 → 身份字段 → 投递备注 → 折叠"技术详情"（DAG 原文/span JSON/归属声明，原生 details 可键盘操作）。
- 人工确认卡与"GitHub 检查"卡补来源行；PR 卡显示真实标题（有记录时）。
- 抽屉：打开焦点移入对话框容器（tabIndex=-1），Esc/遮罩关闭，**关闭后焦点恢复到触发按钮**；rAF 改 setTimeout(0)（内嵌验证环境 rAF 不触发，真实浏览器两者皆可）。
- 侧栏重组："审查"（审查运行）+ "尚未接入"（仓库/RAG/Skill/审批/用量）两组；loopback 细节移出导航。
- 返回列表保留筛选：列表链接携带 `state.from`，详情面包屑返回该 URL。

**批次 C（验证与修复）**：见下表；另修复 390 溢出（顶栏采集范围隐藏、滚动提示改纯渐隐、筛选全宽堆叠）与 text-3 对比度（#69748c→#5e6a84，最差底色 5.02:1）。

## 四、验证结果

**数据语义（status-map.test.mjs 10 项 + 浏览器核对）**：HIGH+COMPLETED≠安全通过 ✓；APPROVED≠已修复/合并 ✓；published+failure 显示"已回写·检查未通过"两事实 ✓；结论缺失≠无问题 ✓；snapshot 标签统一（历史快照）✓；旧 commit 结果独立成行（PR#1 两 head 两行）✓。

**交互（浏览器实测）**：搜索 SHA→1 行且 URL `?q=` ✓；组合筛选+清除按钮 ✓；清除恢复 18 行 ✓；无匹配空态文案 ✓；设筛选→详情→返回 URL/输入框/行数全部保留 ✓；PR 外链新窗口 ✓；SHA 点击复制（title 载完整值）✓；抽屉打开焦点入对话框=true、Esc 关闭=true、焦点恢复触发按钮=true ✓；200% 缩放以 640px 视口近似检查无破版（注：内嵌浏览器无法真实设置 zoom，已如实说明）。

**布局**：1440✓ 1920✓（整行无横滚）1024✓（页面级无溢出 docScrollW=1024，表格内滚动+提示）390✓（docScrollW=390=clientW，溢出已修复）。

**对比度（脚本计算 WCAG AA）**：13 组令牌对全部 ≥4.5:1（修复后 text-3 最低 5.02）。

**回归**：node --test 4 文件 34/34 通过（含真实证据回归与新增语义测试）；vite build 通过。

## 五、截图位置

`.impeccable/review/ui-round2/`：before-1440-runs/detail、after-1440-runs、after-1440-detail-reject、after-1440-detail-live-title（真实 PR 标题）、after-1920-runs、after-1024-runs、after-390-runs。

## 六、未接入 / 未验证项

- **live 模式未接入**（数据采集时间、PR 标题等在 live 下需 C-2/C-3 接口；PR 标题实时化需 GitHub 只读读取，已记 INTEGRATION-REQUESTS C-7）。
- 浏览器 200% 缩放为 640px 视口近似 + 断点推演，未做真实 zoom 截图。
- 抽屉 Tab 焦点圈闭（focus trap）未实现（焦点移入/恢复已做），留 a11y 轮。
- 分页：当前 18 行一屏；数据增长时按现有限流参数（limit/offset 已在 API）加页脚，本轮未做 UI。
- 接口异常态：ErrorBox 路径由 API 层测试覆盖，未做断网截图。
