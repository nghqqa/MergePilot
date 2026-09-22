# MergePilot 管理控制台（Console V0）

面向日常开发者工具的**只读**管理控制台：运行列表 → 运行详情 → 证据/版本信息的真实数据查询闭环。

**数据模式：snapshot** —— 全部数据来自仓库内锁定的真实历史运行证据包（`evidence/`，含 SHA256SUMS），
只读、非实时。live 模式（服务器 PG / MinIO）未接入，页面顶栏有明确标注。

## 启动

```bash
# 方式一：已预构建前端（无需 npm）
node console/backend/server.mjs

# 方式二：前端开发模式（热更新）
cd console/frontend && npm install && npm run dev   # Vite :5177，/api 代理到 :4730
```

访问 <http://127.0.0.1:4730>。服务**只监听回环地址**，不提供任何写操作接口。

环境变量：`CONSOLE_PORT`（默认 4730）、`CONSOLE_HOST`（默认 127.0.0.1）、
`CONSOLE_EVIDENCE_ROOT`（默认 `<repo>/evidence`）。

## 测试

```bash
node --test console/backend/test/pack.test.mjs console/backend/test/runs.test.mjs console/backend/test/api.test.mjs
```

24 项：夹具单元测试（解析/路径穿越防护/完整性校验）+ 真实证据根回归（存在时执行）。

## 结构

```text
console/
├── backend/            # 零第三方依赖 Node 只读 API（沿用 demo-platform 后端模式）
│   ├── server.mjs      # HTTP 路由 + 静态服务
│   ├── lib/pack.mjs    # 证据包访问：安全路径解析、SHA256SUMS 懒校验、run 锚点发现
│   ├── lib/runs.mjs    # 包 → 运行记录归一化（身份/三态/时间线/版本/RAG/用量）
│   └── test/           # node:test（含 fixtures 微型包）
└── frontend/           # React + Vite SPA（设计遵循仓库 DESIGN.md）
    ├── src/pages/      # RunsPage / RunDetailPage（8 标签）/ 未接入 StubPage
    └── dist/           # 预构建产物（已提交，改动后 npm run build 更新）
```

## 边界（如实声明）

- **只有读**：无审批、无派发、无 GitHub 写入、无凭证下发；补丁/证据只可查看与下载。
- **三态分离**：执行状态（投递台账/项目 meta）、审查结论（reviewer 结果）、发布状态（check-run）
  是三个独立事实，各自带来源标注，永不合并为一个"成功"。
- **证据按不可信输入处理**：内容一律转义后纯文本渲染（无 innerHTML / 无脚本执行），
  下载路径经穿越防护必须落在包内。
- **未接入页面**：仓库/RAG 总览/Skill 总览/审批/用量导航如实标注"未接入"并说明依赖，
  不以演示数据填充；run 级的 Skill/RAG/用量信息在详情页展示包内真实记录。
- run-manifest：历史证据包早于该机制（桥自 81e0045 起写入 MinIO），
  版本清单页如实显示"未记录"，后续 run 可从 MinIO 清单接入。

接口契约与跨会话需求见 `docs/productization/console/`（API-CONTRACT / INTEGRATION-REQUESTS / STATUS）。
