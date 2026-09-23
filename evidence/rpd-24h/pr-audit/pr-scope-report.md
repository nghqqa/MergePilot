# PR #233 范围审计（2026-09-24 复核轮）

- PR: #233（OPEN，feat/rpd-24h-delivery → main，head `a14ce1d`）
- merge-base: `e78c5f5`（origin/main）；diff = **157 文件，+19013 / −67**（与 PR 元数据一致）
- CI: 3/3 pass（demo-platform selftest node 18/20/22——这是 MergePilot 仓库自带 workflow 的名称，非 demo-platform 项目文件）

## 分类统计

| 类别 | 文件 | + | − | RPD 归属 | 秘密/凭证/运行数据/历史证据 | 范围扩大风险 | 需拆分 |
|---|---|---|---|---|---|---|---|
| 后端代码(tools+skills) | 74 | 9723 | 58 | RPD-03/04/06 + 历史产品化轮(R3 backfill/M2 审批/v3 骨架/门闭环/接线) | 否 | 否(既定架构边界内) | 见"综合 PR 说明" |
| 测试(tests/) | 45 | 6896 | 9 | RPD-04/05 + 历史轮 | 否 | 否 | 同上 |
| 文档(docs/productization) | 19 | 1874 | 0 | RPD-01 + 历史轮 | 否 | 否 | 同上 |
| RPD/证据 | 17 | 514 | 0 | RPD-01..08 | 否(脱敏输出/判定行,无凭证) | 否 | 否 |
| 仓库配置(.gitignore, pyproject.toml) | 2 | 6 | 0 | 历史轮 / RPD-05 | 否 | 否 | 否 |
| **合计** | **157** | **19013** | **67** | | | | |

## 重点检查结果
- 超大文件: 均为实质源码/测试（gh_bridge 1182 行、pg_runstore 436、test_v3 467 等），**无生成物/二进制/.lock/.min.js/图片**
- demo-platform 或其他无关项目文件: **无**（CI workflow 名含 "demo-platform" 是本仓库自带 workflow）
- .env / token / DSN / 私钥: **无**（仅 `agentteams-cr.env.example`、`docker-compose.cr.example.yml` 占位样例，值为 SUBSTITUTE_AT_DEPLOY）
- 生产环境变更 / controller CR / 共享数据库数据: **无**（migrations 仅 DDL，零数据种子）
- 越界前端/架构改动: **无**（前端=gate_display 八态映射一个纯模块+响应字段；架构=零变更）
- 历史工作树误带入: **无**（启动门禁时工作树 0 条；所有文件可归属 R3..RPD 各轮，详见 unrelated-files.md 结论）

## 综合交付 PR 说明与拆分建议
本 PR = `feat/backend-pg-storage` 分支对 main 的**综合交付**（多轮授权产品化历史 + 本轮 RPD 增量）。
- 方案 A（推荐，现状）: 保留综合 PR——分支历史线性、RPD/evidence 完整、main 落后量大，拆分反而碎片化。
- 方案 B（可选拆分，如用户要求最小 PR）: ① `chore/r3ops-backfill`（r3ops+gitignore+pyproject）→ ② `feat/approval-gate`（approval/console_pg/gh_bridge/tests）→ ③ `feat/case-retrieval-wiring`（case_retrieval+model_gateway+skills）→ ④ `docs/rpd-evidence`。需在新分支重建，本轮未执行。

## 判定
**PR_SCOPE_ACCEPTABLE = true**（无无关文件/无生成物/无秘密/无生产配置/157 文件全部可归属）
