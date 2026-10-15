# MergePilot 管理控制台（Console）

自托管多用户 PR 安全审查工作台的前后端。**multiuser 模式（`MU_MODE=multiuser`）为正式形态**：
webhook 接入 → PR 快照 → 两阶段审查（静态规则 precheck + 可选 LLM 第二阶段）→ 发现落库 →
裁定/修复审批门 → 补丁版本化与验证（可应用性五态 + 实际修复测试）→ 维护者人工确认发布
审查评论到 PR。能力边界与依赖见 [docs/delivery/CAPABILITY-MATRIX.md](../docs/delivery/CAPABILITY-MATRIX.md)。

历史快照（snapshot evidence pack）只读浏览仍保留为 legacy 形态（`evidence/` 证据包 +
SHA256SUMS 校验），multiuser 下不再作为数据源——页面数据源标注 `MU_CANONICAL_LIVE`。

## 启动

```bash
# 方式一：已预构建前端（无需 npm）
node console/backend/server.mjs

# 方式二：前端开发模式（热更新）
cd console/frontend && npm install && npm run dev   # Vite :5177，/api 代理到 :4730
```

访问 <http://127.0.0.1:4730/multiuser>。服务默认只监听回环地址；
生产部署走 [`deploy/selfhost/`](../deploy/selfhost/README.md)（compose 拓扑 + TLS 反代）。

关键环境变量：`MU_MODE=multiuser`、`CONSOLE_PG_DSN`（mu 库；schema 启动自动迁移，
当前 v28）、`CONSOLE_PORT`（默认 4730）、`CONSOLE_HOST`（默认 127.0.0.1）、
`CONSOLE_EVIDENCE_ROOT`（legacy 证据根）。完整清单见
`deploy/selfhost/.env.example` 与 [docs/delivery/DEPLOY-GUIDE.md](../docs/delivery/DEPLOY-GUIDE.md)。

## 主要页面（multiuser）

| 页面 | 内容 |
|---|---|
| 总览 / 待处理 / 仓库 / 审批 | 主审查流程：绑定仓库、运行记录、P0/P1 审批票 |
| 运行记录 → run 详情 | 发现（来源分列 precheck/reviewer）、建议补丁（版本化）、验证三分、发布面板 |
| 组织与接入 | 成员与邀请、GitHub App 安装与仓库绑定、Gitee 连接、运行策略、执行器状态 |
| 设置 / 数据源 / 系统状态 / 诊断 | 实例配置（platform_admin）、出站策略与同意、RAG 模型管理 |

权限即矩阵投影：`my_permissions` 服务端下发（authz.mjs 五角色），无权限按钮置灰并给原因；
`decide_review`（裁定/审批/发布）仅 maintainer，platform_admin 刻意无裁定权。

## 测试

```bash
node --test console/backend/test/
```

99 个测试文件（node:test）：MU 授权矩阵、审查链与 LLM 解耦、审批门 CAS、补丁五态与
fix test、发布幂等、cchain、FXV 执行、集成回归（`*.integration.mjs` 需 PG 等外部依赖，
其余为零依赖单测）。

## 结构

```text
console/
├── backend/
│   ├── server.mjs           # HTTP 路由 + 静态服务（health 暴露 version/git_commit/mu_schema_ready）
│   ├── lib/
│   │   ├── multiuser/       # MU 域：authz（五角色矩阵）/ schema（v28）/ review-service（两阶段审查）
│   │   │                    #   fix-approval（审批门）/ publish（发布，双平台机器人）/ agents/（编排）
│   │   ├── fxv/             # 修复/验证子进程执行底座（dry-run 默认）
│   │   └── pack.mjs 等      # legacy snapshot 证据包访问
│   └── test/                # node:test
└── frontend/                # React + Vite SPA（设计遵循仓库 DESIGN.md）
    ├── src/pages/           # 总览/仓库/审批/组织与接入/运行详情（MuPrDetail*）等
    └── dist/                # 构建产物（canonical 镜像在构建阶段容器内重建，不用工作区 dist）
```

## 边界（如实声明）

- **三态分离**：产物版本（patch_version/digest）、实际执行状态（run status）、验证结果
  （五态/三分/测试证据）、合并资格是四个独立事实轴，永不合并成一个"成功"。
- **远端写仅一处**：维护者人工确认的 PR 评论发布（GitHub App / Gitee 机器人身份，幂等原位更新，
  head 复核防陈旧）。提交代码/合并代码路径本版本结构性不存在（开发中能力，见能力矩阵）。
- **补丁是建议**：修复轮 dry-run 零远端写入；模型建议永不直接出站。
- **凭据红线**：token/私钥仅进程内存瞬时使用；DB/审计/日志零凭据。
- **证据按不可信输入处理**：内容转义纯文本渲染；下载路径穿越防护。
- run-manifest：历史证据包早于该机制的如实显示"未记录"，不伪造。
