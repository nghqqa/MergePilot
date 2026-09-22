# 控制台推进状态（console/STATUS）

**更新**：2026-09-22 ｜ **分支**：`feat/admin-console`（worktree `D:\goai\mp-worktrees\console`，基线 `0ae8843` = `chore/backfill-r3-ops` 已提交顶端）
**负责目录**：`console/`（前后端）+ `docs/productization/console/`。未改动 gh-bridge / workflow-controller / approval / rag / costmeter / 共享数据结构 / r3work。

## 本轮交付：运行查询闭环（snapshot 模式）

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
