# 按我们的 Agent 设计与 Skill 在你自己的环境跑起来

适用读者：想复用 MergePilot 的多 Agent 调度设计、Skill 合同或执行运行时的外部使用者。
按投入由小到大分三级；每一级都给出可独立验证的完成标志。所有命令在仓库根目录执行。

> **边界先行**（与主 README 一致）：RAG 语料 SYNTHETIC/REDACTED；Database Branch 为 SIMULATED
> 隔离模拟；PolarDB NOT CONNECTED；PR Auto Merge DISABLED。Skill 全部 fail-closed：
> 缺少受信 scope 一律 DENY，缺引用来源不得标记"已验证"。本项目未声称生产部署验证。

---

## L1 · 零环境验证（约 2 分钟）

```bash
git clone https://github.com/nghqqa/MergePilot.git
cd MergePilot/demo-platform
node backend/server.mjs          # 打开 http://127.0.0.1:4173
node backend/test/selftest.mjs   # 54 passed, 0 failed
```

- 前端已预构建，无 npm install、无网络依赖；页面全部为**历史已验证运行的回放**（REPLAY 模式）
- 也可以直接下载 [Release v0.2.0](https://github.com/nghqqa/MergePilot/releases/tag/v0.2.0) 的
  `MergePilot-demo.zip`（含预检脚本与现场手册）或观看演示视频
- Python 侧验证：`python -m pip install -e ".[dev]"` 后运行
  `python -m pytest -q tests/demo_console tests/isolated_live tests/verification --import-mode=importlib`
  （1446 passed, 15 skipped；15 项为默认关闭的 PG 验证）

**完成标志**：控制台可打开三条安全路径页面；自测 54/54。

---

## L2 · 单独运行我们的 Skill（约 10 分钟）

Skill 是独立进程，按 **M4-A 公共运行时信封**（`skills/common/schema/`）收发 JSON：
输入须符合各 Skill 的 `schema/input.schema.json`，输出带 name / version /
contract_version / request_id / trace_id；任何越权或缺失 scope 返回 DENY。

| Skill | 代码 | 第三方依赖 | 说明 |
| --- | --- | --- | --- |
| `sast-scan` | `skills/sast_scan/` | `jsonschema`（公共信封校验；扫描逻辑纯标准库） | 静态扫描：危险模式 + taint path join + path traversal |
| `case-retrieval` | `skills/case_retrieval/` | `psycopg2-binary`、`fastembed`（见其 `requirements.txt`） | 仓库级历史案例检索（PostgreSQL + pgvector，建表脚本在 `migrations/`） |

**零依赖快速体验**（无需任何数据库）：

```bash
python -m pip install jsonschema                       # 公共信封校验（唯一依赖）
python -m skills.sast_scan.run --help                  # 打印契约信封
echo '{}' | python -m skills.sast_scan.run             # 空输入 → status: ERROR（fail-closed）
# 按 skills/sast_scan/schema/input.schema.json 构造真实输入后再试
```

**case-retrieval 完整运行**：

```bash
python -m pip install -r skills/case_retrieval/requirements.txt
# 准备 PostgreSQL + pgvector，执行 skills/case_retrieval/migrations/001_case_retrieval_scope.sql
# 连接串、仓库 scope、模型与超时均为部署方环境变量（MERGEPILOT_CR_*），见 SKILL.md 的 Trust boundary
python -m skills.case_retrieval.run
```

**完成标志**：两个 Skill 均输出契约信封；case-retrieval 在真实 pgvector 上按
source_refs 返回检索结果；越权输入返回 DENY。

Skill 的调用方（我们的场景是 CoPaw worker 的工具调用，见 L3）只按本合同通信——
你可以把同一个合同接到自己的 Agent 框架上，不需要我们的运行时。

---

## L3 · 完整多 Agent 闭环（你自己的 AgentTeams 环境）

### 框架与运行时

- 框架基点：**AgentTeams（HiClaw）**（Manager / Worker / Matrix / MinIO / Worker 隔离；
  归属与替代方案见 [THIRD_PARTY.md](../THIRD_PARTY.md)）
- Worker/Manager runtime 为官方枚举：`openclaw` / `copaw` / `qwenpaw`——我们的三条案例
  跑在 **CoPaw worker**（嵌入式 Manager + 4 worker 容器）上；Controller 原生支持按 Worker
  声明选择 runtime 镜像（qwenpaw 为官方默认 runtime，我们完成过 14 项兼容性审计，可并行接入）
- 宿主：Docker Desktop for Windows（项目早期运行于 WSL2，因环境兼容性迁移；详见主 README「主项目」一节）

### 两条入口

1. **隔离栈离线镜像包**：[v0.1.0-preview.4 Release](https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.4)
   下载 `images-oci.tar` + `manifest.json`，校验 checksums 后 `docker load`，按
   `bootstrapper.ps1` 引导（受支持环境为 Windows 11 + WSL2）。
   ⚠️ 该包只含**隔离栈 9 个服务镜像**（controller / policy-gateway / pgvector / gh-proxy /
   gh-webhook / mcp-bridge / preflight / demo-console / console-edge），**不含 Agent 执行运行时镜像**
   （CoPaw worker / 嵌入式 Manager——见下方“运行时镜像的分发状态”）
2. **从源码搭建（早期文档）**：[docs/环境搭建-HiClaw-WSL.md](环境搭建-HiClaw-WSL.md)
   记录了 AgentTeams + Element Web + Worker 的完整搭建过程。⏳ 该文写于 WSL2 时点，
   命令与模型配置已过时，概念与拓扑仍适用

### 运行拓扑与外部服务清单

Controller（Python，状态机 / DAG 派发 / CAS / 超时 HOLD / 回滚）+ Policy Gateway
（ALLOW/DENY/HOLD）+ GitHub MCP（隔离服务，PAT 不进 Worker）+ MinIO（任务与证据存储）
+ Matrix / Element（触发与通信）+ LLM 网关（Higress 或自建；consumer-token 隔离）
+ CoPaw worker ×4（reviewer / fixer / verifier / manager 角色）。

需要准备：DeepSeek 兼容 API key、GitHub PAT（仅 MCP/gh-proxy 使用）、MinIO 凭据、
Matrix 账号。六类 Skill 以 Schema、deadline、错误码与 fail-closed 合同执行；
合同总览见 [SKILLS.md](../SKILLS.md)（提交材料目录亦有副本）。

**注意**：我们对 CoPaw 运行时的本地修改（taskflow / matrix_channel 等 5 个文件，
HIGH-RISK-FIX 阶段成果）目前**未随 Release 公开分发**——单镜像体积超过 GitHub 单资产 2GiB 上限，且 preview.4 的
`images-oci.tar` 只含隔离栈。验证这些运行时的审计记录（manager 拓扑、镜像 digest、容器连接性）
见演示平台 Audit 页；如需运行时镜像或源码级复用，请提 issue 说明用途。

### 完成标志

Matrix 触发一条任务 → Controller 按 DAG 供给 worker → Reviewer 分级 →
（高危）人工门 → Fixer → Verifier 探针 → 证据写 MinIO + AgentLoop 云端 Trace。

---

## 义务

Apache-2.0。复用时请保持 fail-closed 行为、最小权限边界与"未验证之事不得声称"的
口径——这也是本仓库 [CONTRIBUTING](../CONTRIBUTING.md) 与 [AGENTS.md](../AGENTS.md)
对 all collaborators 的要求。
