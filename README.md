# MergePilot：多 Agent PR 审修闭环

**多 Agent 代码与数据库变更安全闭环系统**

普通变更，AI 自主完成。高危变更，系统停下等人工。

[![AgentLoop Cloud Trace](https://img.shields.io/badge/AgentLoop_Cloud_Trace-VERIFIED_LIVE_CLOUD-brightgreen)](#agentloop-云端-trace)
[![Three Safety Paths](https://img.shields.io/badge/Three_Safety_Paths-VERIFIED-brightgreen)](#三条安全决策路径)
[![RAG](https://img.shields.io/badge/RAG-SYNTHETIC_DEMO-blue)](#rag-检索能力)
[![Database Branch](https://img.shields.io/badge/Database_Branch-SIMULATED-yellow)](#polardb-与-branch-边界)
[![PolarDB](https://img.shields.io/badge/PolarDB-NOT_CONNECTED-red)](#polardb-与-branch-边界)
[![PR Auto Merge](https://img.shields.io/badge/PR_Auto_Merge-DISABLED-red)]
[![selftest](https://github.com/nghqqa/MergePilot/actions/workflows/selftest.yml/badge.svg)](https://github.com/nghqqa/MergePilot/actions/workflows/selftest.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

## 演示

**Demo 视频（75 秒 · 1080p · 中文旁白）**：[Watch Demo](https://github.com/nghqqa/MergePilot/releases/download/v0.2.0/MergePilot-demo.mp4)

**在线体验**：`git clone` 后执行 `cd demo-platform && node backend/server.mjs`，访问 `http://127.0.0.1:4173`。零第三方依赖，无需 `npm install`。也可下载离线包 [MergePilot-demo.zip](https://github.com/nghqqa/MergePilot/releases/download/v0.2.0/MergePilot-demo.zip)（约 1 MB）。

## 要解决的问题

AI 改代码很快，但企业不敢让它碰生产环境：出了高危漏洞谁负责？改坏了怎么追溯？MergePilot 解决的是"敢让 AI 改"这件事——每一步有审计，高危必须人工点头，拒绝就真的停下。

## 三条安全决策路径

| 路径 | 案例 | 行为 | 验证点 |
| --- | --- | --- | --- |
| **自主完成** | PR #1（普通变更） | AI 审查 → 修复 → 验证，全自动完成，无人工介入 | 项目 completed · PR 保持 OPEN |
| **批准后完成** | PR #2（高危 · CWE-22） | 人工安全门 → 批准 → 修复 → 探针验证（200→404） | 修复有效 · PR 保持 OPEN |
| **拒绝后停止** | PR #3（严重 · CWE-78） | 人工拒绝 → 永久 BLOCKED → 409 终态 | 拒绝不可翻转 · 审计归档 |

## Agent 协同设计

Reviewer、Fixer、Verifier 三个 Agent 职责分离、互相制衡：

- **Reviewer** 只做语义判断与风险分级（NORMAL / HIGH / CRITICAL 三级；`benchmark/` 数据集对同一维度标注为 L0 / L1 / L2），不修改代码
- **Fixer** 在被派发前保持 LOCKED，高危时必须等人工门放行
- **Verifier** 用独立探针复核修复效果（不信任 Fixer 的自述），结果写入 MinIO 证据
- **人工安全门**：高危时系统自动暂停（Fixer/Verifier LOCKED），批准或拒绝均为真实人工记录，拒绝后 409 终态不可翻转
- **DAG 依赖**：review-1 → fix-1 → verify-1，逐级锁定防止越权

Agent 只承担语义判断，六类 Skill 以 Schema、deadline、错误码和 fail-closed 合同执行。Workflow Controller 负责状态机、确定性交接、CAS、超时 HOLD 和回滚；Policy Gateway 负责 ALLOW/DENY/HOLD；GitHub MCP 是隔离服务，PAT 不进入 Worker。

![MergePilot 架构：Agent 只做语义判断，Workflow Controller、Policy Gateway 与审计事实构成确定性控制面](docs/assets/readme/preview4/architecture-preview4.png)

源图（可编辑 SVG）：[`docs/assets/mergepilot-architecture.svg`](docs/assets/mergepilot-architecture.svg)

## AgentLoop 云端 Trace

全流程已接入阿里云 AgentLoop，Agent + LLM + Tool 三类 span 合并在同一条链路。

**权威 Trace**：`fbf4a3cec0493990d76e10a102418be1`（真实 CoPaw 运行，17.6s）

| 指标 | 值 |
| --- | --- |
| Agent 调用 | 1 |
| LLM 调用 | 24 |
| 工具调用 | 8 |
| 总 Token | 51,890（入 49,867 / 出 2,023） |
| 模型 | deepseek-chat |
| 会话 ID | N2KQqHVSBsSZc9utWsEeZ5f |

span 父子关系：`agent_step → invoke_agent → { chat deepseek-chat（原生）+ genai.llm.call（loongsuite 包装）+ tool.projectflow + tool.taskflow + matrix.send }`。原生 agentscope span 与 loongsuite 包装 span 共享全局 TracerProvider，天然合并。

## RAG 检索能力

内置 8 篇合成演示文档（9 分块），支持自然语言检索并返回引用来源。

- 每次检索返回 `query_hash`（不保存原文）、`top_k`、逐条 `{document_id, chunk_id, score, source_ref}`
- **无引用来源的答案不会被标记为已验证**
- 数据模式为 SYNTHETIC/REDACTED（合成演示集，非企业语料）
- 嵌入：本地确定性 hash-bow-256（无外部服务依赖）

## PolarDB 与 Branch 边界

| 项 | 状态 |
| --- | --- |
| PolarDB | **NOT CONNECTED**（8 项接入门槛已在代码中预留） |
| Database Branch | **SIMULATED**（隔离模拟环境上的验证状态机） |
| PR Auto Merge | **DISABLED** |

支持 create_branch → validate_migration → assert_data → rollback_check 全链路（当前在模拟 fixture 上运行）。真实 PolarDB 接入需满足 8 项门槛后由环境变量切换。

## 快速开始

### 演示平台（推荐，零依赖）

```bash
git clone https://github.com/nghqqa/MergePilot.git
cd MergePilot/demo-platform
node backend/server.mjs        # 打开 http://127.0.0.1:4173
```

Node.js ≥ 18 即可，无需 `npm install`：前端已预构建（`frontend/dist/`），三个案例的回放证据随仓库分发（`evidence/PHASE14-*`，SHA256 锁定）。Windows 也可直接双击 `demo-platform/start-demo.bat`。

自测：`node backend/test/selftest.mjs`（54 项：脱敏、回放完整性、API 契约、无泄密扫描；[CI 在 Node 18/20/22 上自动运行](https://github.com/nghqqa/MergePilot/actions/workflows/selftest.yml)）。

不想 clone？下载 [MergePilot-demo.zip](https://github.com/nghqqa/MergePilot/releases/download/v0.2.0/MergePilot-demo.zip)（约 1 MB，已含证据、预检脚本与现场演示手册），解压后进入 `MergePilot-demo-现场版` 目录，双击 `start-demo.bat` 或执行 `node MergePilot-demo/backend/server.mjs`。PPT、视频与完整提交包见 [Release v0.2.0](https://github.com/nghqqa/MergePilot/releases/tag/v0.2.0)。

### 主项目（Python 控制面）

完整隔离栈（Controller、Policy Gateway、GitHub MCP、PostgreSQL/pgvector、MinIO）以离线镜像包发布：从 [v0.1.0-preview.4 Release](https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.4) 下载资产，校验 checksums 后按 `bootstrapper.ps1` 引导执行——该引导器在发布时点受支持的环境为 **Windows 11 + WSL2**（栈内服务运行于 WSL2 发行版中的 Docker）。

项目最初即运行在 WSL2 上；后续因 WSL2 环境兼容性问题，将整套执行环境迁移至 **Docker Desktop for Windows** 并跑通全流程：Controller、MinIO、LLM Gateway 与多 Agent 运行时（Reviewer / Fixer / Verifier 的 Copaw worker，由 Controller 统一 reconcile）均在该引擎内运行——三条案例的回放数据即产自该环境（演示平台 Audit 页可见对应组件与证据）。

仓库内的 `docker-compose.yml` 与 `Dockerfile.*` 是隔离栈镜像的构建配方，由 `tools/demo_console/one_click_startup.py` 编排调用，不支持直接 `docker compose up`。源码开发与本地测试命令见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 仓库结构

```
MergePilot/
├── demo-platform/       # 演示平台：前端 + 后端 + RAG + PolarDB 模拟适配器
│   ├── backend/         # 零依赖 Node 后端（server + API + RAG + PolarDB 边界）
│   ├── frontend/        # React 控制台（含预构建 dist）
│   ├── evidence-adapter/# 只读数据适配层 + 内置演示数据集
│   ├── rag-data/        # RAG 合成数据集
│   ├── SKILLS.md        # 核心 Skill 清单
│   └── test/            # 自测脚本（54 项）
├── evidence/            # 回放证据子集（SHA256 锁定）
├── shared/              # 数据契约
├── docs/                # 文档与架构图
├── skills/              # Agent Skill 定义
├── config/              # 配置
└── LICENSE              # Apache 2.0
```

## 评估方法

结果评估与轨迹评估**分开执行、分别呈现**：

- **结果评估**（最终状态）：关联仓库识别、影响表识别、历史风险命中、候选结论、数据核对、回滚检查、人工门终态
- **轨迹评估**（过程顺序）：先检索 RAG、正确选择工具、创建隔离 Branch、拒绝后锁定、source_refs 保留、Agent→LLM→Tool Trace 形成

即使最终状态正确，若过程顺序违规（如未检索先动手、跳过人工门），轨迹评估仍判不通过。

## 技术栈

- **主项目**：Python（pyproject.toml）· 2471 tests · 6 类 Skill · 4 Agent 承载 6 类职责
- **演示平台**：Node.js 零依赖后端 + React 前端（Vite 预构建）
- **可观测**：OpenTelemetry GenAI 语义约定 · loongsuite 探针 · 阿里云 AgentLoop

## 提交材料

- [`submission/`](submission/) — 决赛提交文档：SKILLS.md（分级如实口径）、跨仓 Schema 审查 Skill、演示平台 DEPLOY 说明与 `.env.example`、路演备用网页版（含同版 PDF）
- `demo-platform/` 已同步决赛现场版修复：PR#3 风险等级在 Operations 视图的错误显示、Evidence 视图字段错配、站内路由链接、Audit 组件表与 SKILLS.md 口径统一为「已云端确认样本 / 本地契约审计」分级表述

## 许可

[Apache License 2.0](LICENSE)
