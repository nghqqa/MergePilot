# MergePilot Demo

MergePilot 是一个面向企业代码和数据库变更的多 Agent 审查与验证平台。本包是一个可本地运行的离线演示：前端控制台 + 零依赖 Node 后端 + 内置演示数据集。

## 环境要求

- Node.js ≥ 18（后端零第三方依赖；仅重建前端时需要 npm）
- 现代浏览器（Chrome / Edge / Firefox）

## 安装依赖

无需安装即可运行（前端已预构建）。如需重新构建前端：

```bash
npm run build
```

## 启动命令

```bash
# 方式一：直接启动
node backend/server.mjs

# 方式二：Windows 双击 start-demo.bat；macOS/Linux 执行 ./start-demo.sh

# 方式三：npm
npm start
```

启动后访问 <http://127.0.0.1:4173>。

**现场演示**：本目录与 `evidence/` 的上层是"现场版"启动包——含 `start-demo.bat` / `start-demo.sh` / `preflight-demo.ps1`，演示动线与口播见上层 `README-现场演示.md`。

离线自检（可选）：

```bash
npm run selftest
```

## Demo 页面

| 页面 | 内容 |
| --- | --- |
| 总览 | 演示导览与全局状态 |
| PR #1 | 普通代码变更：Reviewer → Fixer → Verifier 自动完成 |
| PR #2 | 高风险发现（CWE-22 路径穿越）：人工批准后才继续修复与验证 |
| PR #3 | 严重风险：人工拒绝后安全停止，后续 Agent 保持锁定 |
| PR #4 | 跨仓 Schema 变更：候选迁移方案进入隔离验证，失败候选被淘汰 |
| RAG | 检索演示：识别关联仓库、历史 Finding 与变更风险 |
| Operations / 审计 | 任务来源、数据模式、完整性与边界说明 |
| `SKILLS.md` | 核心 Skill 清单：用途 / 输入输出 / 调用条件 / 失败处理 / 安全边界 / 复用价值 |

演示控制条支持逐步回放每个 PR 的事件序列（重置 / 播放 / 下一事件 / 终态 / 人工门）。

## 数据模式

- 本包内置一套脱敏/合成演示数据集（`evidence-adapter/demo-data/`），后端以只读回放方式提供，不连接外部服务，不修改任何真实 PR 或数据库。
- 数据完整性：服务启动时对内置数据逐文件重算 SHA256 并与内置清单比对（见 `/api/health`）。
- RAG：合成演示数据（SYNTHETIC）；数据库 Branch：隔离模拟环境（SIMULATED）；真实 PolarDB：未连接。
- PR 自动合并：默认关闭。演示中的操作不会产生任何真实写操作。

## 已知限制

- 演示数据为脱敏/合成内容，仅用于展示系统行为，不代表生产数据规模。
- 数据库验证运行在隔离模拟环境；真实 PolarDB 接入在路线图中，当前未连接。
- 页面中的 Live 模式仅在配置了本地 AgentTeams runtime 环境变量时可用；未配置时如实显示不可用，不使用假数据补齐。

## 目录结构

```text
MergePilot-demo/
├── README.md
├── LICENSE
├── package.json
├── start-demo.bat / start-demo.sh
├── backend/            # 零依赖 Node 后端（server、API、RAG、PolarDB 边界）
├── SKILLS.md           # 核心 Skill 清单
├── evidence-adapter/   # 只读数据适配层 + 内置演示数据集
├── shared/             # 数据契约
├── frontend/           # React 控制台（含预构建 dist）
└── ../evidence/        # 回放证据子集（与 MergePilot-demo/ 保持并列；平台按相对路径只读加载）
```

> 请保持解压后的目录结构：`MergePilot-demo/`（代码）与 `evidence/`（回放证据）并列。
> 离线自测：`node backend/test/selftest.mjs` → 54 passed, 0 failed。

## 许可证

MIT，详见 [LICENSE](LICENSE)。
