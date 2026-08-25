# MergePilot

> 多 Agent PR 审修与风险治理闭环：把审查、修复、验证、L2 审批、合并与回滚放进确定性控制面，并留下可审计的结构化事实。

[![Release](https://img.shields.io/badge/release-v0.1.0--preview.3-orange)](https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.3)
[![Tests](https://img.shields.io/badge/tests-2246%20passed%20%2F%2020%20skipped-blue)](docs/复赛材料/04-声明证据矩阵/声明证据矩阵.md)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

MergePilot 面向“LLM 能提出建议，但不能独自承担工程控制面”的问题。高风险操作必须经过 Policy Gateway 与人工审批；任何环节不确定时 fail-closed，失败后可以沿审计事实执行 revision-cut / rollback。

> **当前公开范围**：Apache-2.0 开源 Preview，版本 `v0.1.0-preview.3`，merged main commit `379744d`。同机安装、生命周期、只读控制台和本地真实 E2E 已验收；独立物理机验收仍为 `EXTERNAL_BLOCKED`。Preview 不等于生产验证。

## 先看什么

下载试用： [v0.1.0-preview.3 Release](https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.3)

安装与回退： [Preview 代码包说明](docs/复赛材料/02-代码包说明/README.md)

运行 Demo： [现场与录屏脚本](docs/复赛材料/03-Demo/现场与录屏脚本.md)

复赛方案： [更新版项目方案 PDF](docs/复赛材料/01-更新版项目方案/MergePilot-复赛更新版项目方案-v1.pdf)

声明核对： [声明证据矩阵](docs/复赛材料/04-声明证据矩阵/声明证据矩阵.md)

---

## 解决什么问题

| 工程治理问题 | MergePilot 的确定性机制 |
|---|---|
| 受保护分支和路径可能被越权修改 | Policy Gateway 对工具调用给出 ALLOW / DENY / ERROR，命中保护前缀立即拒绝 |
| 高风险合并不能只依赖 Agent 判断 | L2 approval ticket、单次有效授权与 SHA/CAS 校验 |
| 多 Agent 阶段可能乱序、重复或崩溃 | PostgreSQL 状态机、Outbox、幂等事件与可恢复阶段推进 |
| 审批后 revision 发生漂移 | approved / observed SHA 对比，检测 `REVISION_DRIFT` 后 fail-closed |
| 合并或复验失败需要可恢复 | revision-cut / rollback + re-verify，状态收敛到 `RECOVERED` |
| 结果难以审计 | `mcp_calls`、`audit_events`、rollback facts 与只读 snapshot |

设计立场：**Agent 负责语义判断；Skill 负责有 Schema、deadline 和失败闭合合同的工程动作；确定性控制面负责权限、状态、证据与失败恢复。**

## Preview 事实卡

冻结门禁：`2246 passed / 0 failed / 20 skipped`。

历史门禁里程碑：81 passed（Phase 1-G）→ 60 passed（M4-B）→ 50 passed（M4-C）→ 31 passed（M4-D）→ 1440 passed / 15 skipped / 0 failed（M5-0D）→ 12 → 12（showcase 对称性）→ 11 PASS / 0 FAIL（M8 端到端）→ PREFLIGHT_OK → 2246（preview.3 基线）。

离线交付包含 9 个镜像，`images-oci.tar` 约 847.4MB；Windows 发布边仅监听 `127.0.0.1:8600` 和 `127.0.0.1:8090`。

传输档案为 `wsl-user-relay`，`direct_routing_verified=false`。`SAME_MACHINE_ACCEPTED` 只表示声明范围内的同机验收，不等于独立物理机或生产验收。

---

## 系统架构

<p align="center">
  <a href="docs/showcase/architecture.svg">
    <img src="docs/showcase/architecture.svg" alt="MergePilot isolated-live architecture" width="100%">
  </a>
</p>

架构按职责拆成四条清晰链路：

- **控制链**：PR → Policy Gateway → Controller；
- **事实链**：Controller / Gateway → PostgreSQL 审计状态库；
- **展示链**：read-only snapshot → Demo Console → console-edge → loopback browser；
- **启动门禁**：Preflight 独立执行 10 项合同检查，全部通过才输出 `PREFLIGHT_OK`。

关键边界：console-edge 只是 publication plumbing；deterministic seed 只用于展示；Demo Console 只读；M8-A1 不等于 revision producer integration。

---

## 三个确定性案例

三个案例均来自 `tools/demo_console/showcase_cases.py`，基于合成仓库 `mergepilot/showcase-demo` 的确定性种子数据。

| 案例 | 治理链路 | Policy / Evidence | 最终结果 |
|---|---|---|---|
| **A · Protected Merge Success** | review → fix → verify PASS → merge | ALLOW + L2 ticket + five-step audit | **MERGED** |
| **B · Fail-Closed Rejection** | review → fix DENIED，时间线立即终止 | `PROTECTED_PATH_PREFIX` + policy_deny | **FAIL** |
| **C · Revision Drift Recovery** | merge → drift-check → rollback → re-verify | `REVISION_DRIFT` + rollback facts | **ROLLED_BACK / RECOVERED** |

**A · Protected Merge Success** — `run-showcase-a` · PR `#101` · L2 `tkt-showcase-a-l2`

- `case_id=case-showcase-protected-merge-success`；Policy INTENT / RESULT 均为 `ALLOW`，最终状态 `MERGED`。
- base `73686f77636173652d612d626173650000000000` · head `73686f77636173652d612d686561640000000000` · merge `73686f77636173652d612d6d6572676500000000`

**B · Fail-Closed Rejection** — `run-showcase-b` · PR `#102`

- `case_id=case-showcase-failclosed-policy-rejection`；`create_or_update_file` 命中 `samples/`，Policy 返回 `DENY / PROTECTED_PATH_PREFIX`，时间线立即终止，最终状态 `FAIL`。
- base `73686f77636173652d622d626173650000000000` · head `73686f77636173652d622d686561640000000000`；无 merge SHA、无后续 verify / merge 阶段。

**C · Revision Drift Recovery** — `run-showcase-c` · PR `#103` · L2 `tkt-showcase-c-l2`

- `case_id=case-showcase-revision-drift-recovery`；`REVISION_DRIFT` 触发 rollback，re-verify `PASS`，最终 `ROLLED_BACK / RECOVERED`。
- approved head `73686f77636173652d632d686561640000000000` · merge `73686f77636173652d632d6d6572676500000000` · observed drift `73686f77636173652d632d647269667400000000` · recovered `73686f77636173652d632d7265636f766572656400000000`

---

## Showcase 控制台截图

截图来自隔离栈的 live snapshot：CSS viewport 为 1440×900、`deviceScaleFactor=2` 捕获，PNG 像素尺寸为 2880×1800。

<table>
  <tr>
    <td width="50%"><strong>01 · Overview</strong><br>运行身份、PR、SHA 与最终状态<br><img src="docs/showcase/presentation/desktop-01-overview@2x.png" alt="Overview page" width="100%"></td>
    <td width="50%"><strong>02 · Timeline</strong><br>阶段顺序、owner、verdict 与时间<br><img src="docs/showcase/presentation/desktop-02-timeline@2x.png" alt="Timeline page" width="100%"></td>
  </tr>
  <tr>
    <td width="50%"><strong>03 · Findings</strong><br>Policy DENY 与 fail-closed 事实<br><img src="docs/showcase/presentation/desktop-03-findings@2x.png" alt="Findings page" width="100%"></td>
    <td width="50%"><strong>04 · RAG Advisory</strong><br>真实 `not_measured` 能力边界<br><img src="docs/showcase/presentation/desktop-04-rag@2x.png" alt="RAG advisory page" width="100%"></td>
  </tr>
  <tr>
    <td width="50%"><strong>05 · Trace Tree</strong><br>Gateway INTENT / RESULT 决策链<br><img src="docs/showcase/presentation/desktop-05-trace@2x.png" alt="Trace page" width="100%"></td>
    <td width="50%"><strong>06 · Policy &amp; Safety</strong><br>ALLOW / DENY 汇总与 rollback facts<br><img src="docs/showcase/presentation/desktop-06-safety@2x.png" alt="Safety page" width="100%"></td>
  </tr>
  <tr>
    <td width="50%"><strong>07 · Evidence</strong><br>L2 ticket、audit summary 与完整性<br><img src="docs/showcase/presentation/desktop-07-evidence@2x.png" alt="Evidence page" width="100%"></td>
    <td width="50%"><strong>08 · Benchmark</strong><br>诚实展示 `NOT_MEASURABLE_WITH_CURRENT_RUNTIME`<br><img src="docs/showcase/presentation/desktop-08-benchmark@2x.png" alt="Benchmark page" width="100%"></td>
  </tr>
</table>

### Mobile · CSS viewport 390×844

移动端同样以 DPR=2 捕获，PNG 像素尺寸为 780×1688。

<table>
  <tr>
    <td width="50%"><strong>Overview · Case A</strong><br><img src="docs/showcase/presentation/mobile-01-overview@2x.png" alt="Mobile overview" width="100%"></td>
    <td width="50%"><strong>Timeline · Case B</strong><br><img src="docs/showcase/presentation/mobile-02-timeline@2x.png" alt="Mobile timeline" width="100%"></td>
  </tr>
  <tr>
    <td width="50%"><strong>Safety · Case C</strong><br><img src="docs/showcase/presentation/mobile-03-safety@2x.png" alt="Mobile safety" width="100%"></td>
    <td width="50%"><strong>Evidence · Case C</strong><br><img src="docs/showcase/presentation/mobile-04-evidence@2x.png" alt="Mobile evidence" width="100%"></td>
  </tr>
</table>

---

## 8 页面控制台

只读运维控制台（`/e2e-status.html`）：深色导航 + 浅暖工作区、17 阶段时间线、五项真实性边界、六边路由矩阵。

- 9 状态诚实渲染（loading/unavailable/empty/running/complete/failed/stale/malformed/partial）
- 10s 自动刷新 + 手动刷新（去重/超时/可见性暂停/陈旧标记）
- Windows loopback 发布边仅监听 127.0.0.1:8600/8090

不声称完整 WCAG 合规；residual validation 记录在声明证据矩阵。

## Quick Start

### Release Preview

评委和试用者应优先使用 [v0.1.0-preview.3 Release](https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.3)。下载后先验证 `checksums.sha256`，再按包内 README 执行 `Check → Install → Doctor → Start → Status`。Preview 仅支持 Windows loopback，不是生产部署包。

### 源码 Showcase

### 前置条件

- Windows + WSL2；Docker daemon 通过 `unix:///var/run/docker.sock`；
- 宿主 Python 3.12；镜像基于 `python:3.12-slim` 与 digest 固定的 `pgvector/pgvector`；
- 真实 PostgreSQL 测试仅在 `EPHEMERAL_PG_VERIFY=1` 时执行，默认保持 unset。

### 最小 CLI(开发预览)

六命令本地操作入口([文档](docs/mergepilot-cli.md)):`install` / `doctor` /
`start` / `status` / `stop` / `cleanup`。它把 `one_click_startup.py` 的版本化
计划生成器接到真实执行器,带写前 journal、逆序 rollback、manifest 原子更新与
凭据零泄漏合同;仅支持 Windows 10/11 + WSL2 `MergePilot-Test` 隔离开发预览,
不是 GitHub App、生产验证或 SaaS。

```bash
pip install -e .
mergepilot doctor                 # 只读体检(发行版必须已 Running,绝不隐式启动)
mergepilot install                # 构建 5 个本地镜像并记录真实 image ID
mergepilot start --run-id run-showcase-a   # 全栈启动,断言 PREFLIGHT_OK
mergepilot status                 # absent / partial / healthy
mergepilot stop                   # 删除会话容器/网络/秘密,保留镜像
mergepilot cleanup --apply        # stop + 删除已核验镜像与 install manifest
```

### 构建镜像(不走 CLI 时)

```bash
docker build -f Dockerfile.policy-gateway -t mergepilot-isolated-policy-gateway:local .
docker build -f Dockerfile.controller -t mergepilot-isolated-controller:local .
docker build -f Dockerfile.demo-console -t mergepilot-isolated-demo-console:local .
docker build -f Dockerfile.console-edge -t mergepilot-isolated-console-edge:local .
docker build -f Dockerfile.preflight -t mergepilot-isolated-preflight:local .
```

完整启动 argv 由 `tools/demo_console/one_click_startup.py` 生成：网络 → PostgreSQL → Gateway → Controller → Demo Console → Edge → Preflight。密码和 DSN 只通过 0600 secret 文件传递，不进入 argv 或日志。

可选择 `run-showcase-a`、`run-showcase-b` 或 `run-showcase-c` 作为 Demo Console 的 `run_id`。浏览器只访问 loopback publication：`http://127.0.0.1:8600`。

### Seed 与清理

```bash
python tools/demo_console/showcase_cases.py > <showcase-seed.sql>
# 使用你自己的临时管理连接，以 --single-transaction 和 ON_ERROR_STOP=1 注入
```

清理计划同样由 `one_click_startup.py` 生成，覆盖容器、匿名卷、internal network 与 publication bridge。

### 不需要 Docker 的回归

```bash
python -m pytest -q tests/demo_console tests/isolated_live tests/verification --import-mode=importlib
```

---

## 演示脚本

5 分钟演示流程见 [`docs/showcase/demo-script.md`](docs/showcase/demo-script.md)。

---

Agent 负责语义判断——不是自主任务分解；Skill 负责有 Schema、deadline 和失败闭合合同的工程动作；确定性控制面负责权限、状态、证据与失败恢复。

Worker 侧 TASK_COMPLETED handoff 回路已于 2026-08-18 通过隔离栈验证（恢复性提醒：这是隔离栈 fixture 验证，不是生产集成）。AgentTeams 仍是多 Agent 协同与任务编排基座。

M8-A2-a 已通过隔离六容器 fixture 验证；M8-A2-b Policy→审计可复算；M8-A2-c revision-cut rollback + re-verify 收敛到 RECOVERED。

## 测试与真实性边界

### 已经证明的部分

- 公开 Preview 冻结门禁：**2246 passed / 0 failed / 20 skipped**。
- run35 本地真实 E2E：**17 个阶段完成、16/16 前置检查通过、6/6 路由边通过**。
- 同一台 Windows 11 + WSL2 机器上，安装、启动、状态检查、停止、回滚和清理流程已经跑通。
- 只读控制台能够区分 `complete`、`failed` 和 `stale`，并展示首个稳定错误、Receipt 与 Matrix 结果。
- Showcase 的成功、拒绝和 revision drift recovery 是可重复的合成演示，用于解释控制面行为。

### 尚未证明的部分

| 边界 | 通俗说明 |
|---|---|
| `application_integration_verified=false` | 尚未接入真实业务应用并完成验收。 |
| `database_verified=false` | PostgreSQL 等组件已在隔离环境运行，但不能据此声称生产数据库已验证。 |
| `production_verified=false` | 当前是 Preview，不是生产部署验证。 |
| `revision_producer_contract=NOT_VERIFIED` | revision 生产者合同尚未在完整目标环境完成最终验证。 |
| `audit_producer_contract=NOT_VERIFIED` | 审计生产者与持久化链路尚未在完整目标环境完成最终验证。 |
| `direct_routing_verified=false` | 当前路径经过 `wsl-user-relay`，不是内核直连路由。 |

### 如何理解这些结果

`SAME_MACHINE_ACCEPTED` 表示同机 Preview 验收通过；`EXTERNAL_BLOCKED` 表示独立 Windows 11 物理机验收仍未完成。两者都不等于生产可用。

历史 M8 运行过程、协议细节和逐项测试数字保留在 [历史运行记录](docs/README-历史运行记录.md) 与 [证据目录](evidence/) 中，GitHub 首页不再展开内部协议日志。

---

## 文档与贡献

本 README 是公开项目的当前入口与真实性边界说明；[文档索引](docs/README.md) 说明各类材料的用途和时效。

- 新贡献者请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；面向 Agent 的仓库规则见 [AGENTS.md](AGENTS.md)。
- 当前设计与使用入口：本 README、[Showcase 演示脚本](docs/showcase/demo-script.md) 与 [Isolated-live 设计记录](docs/ISOLATED-LIVE-PG-Ephemeral-Verification-Design.md)。
- 历史里程碑和竞赛提交快照：[项目状态记录](docs/项目状态.md)、[初赛证据索引](docs/初赛证据索引.md)、[初赛声明—证据矩阵](docs/初赛声明-证据矩阵.md)、[历史运行记录](docs/README-历史运行记录.md)。这些文档保留当时结论，不是当前能力的独立声明。
- 可复现性与归档材料：[benchmark 摘要](benchmark/formal-summary.md)、[`evidence/`](evidence/)（历史验证资产）与 [`verification/`](verification/)（当前验证约定）。

---

## 仓库结构

```text
MergePilot/
├── tools/
│   ├── policy-gateway/       # Policy、L2 approval、INSERT-only audit
│   ├── workflow-controller/  # 状态机、Outbox、恢复与 rollback
│   ├── audit-db/             # PostgreSQL 审计 schema 与权限
│   └── demo_console/         # 8-page live console + console-edge
├── tests/                    # 业务合同、负向矩阵、组件与材料测试
├── docs/showcase/            # 架构图、演示脚本与 @2x 展示资产
└── README.md
```

## 许可

[Apache License 2.0](LICENSE)。
