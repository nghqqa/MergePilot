# MergePilot · README 历史运行记录（归档）

> 本文件归档 README 早期的开发期排障与运行经验，保留原始信息，不再作为当前能力表述。
> 当前权威状态以 [`初赛声明-证据矩阵.md`](初赛声明-证据矩阵.md) 与 [`项目状态.md`](项目状态.md) 为准；
> 可复现的最短路径见 [README 快速开始](../README.md#快速开始)。

---

## 1. 当前环境的运行时经验（2026-07-23）

**现象**：在当前本地 AgentTeams v1.1.2 环境中，默认 QwenPaw Manager 出现消费者/会话处理不可靠。

**处理**：安装时将 Manager 运行时切换为 OpenClaw。该配置在本环境中让 review→fix→verify 处理稳定；这是环境范围内的验证结论，不作为所有版本和部署的普遍要求。

**验证**：全 OpenClaw 架构（Manager + 3 独立 Worker）完成双场景：PR #42 发现 6 项问题并最终 MERGE（人审后）；PR #43 发现 4 项问题、确认 3+ 已知 CVE，裁定 HOLD 并明确 Do not merge。

---

## 2. GitHub MCP 接入排障（2026-07-24）

**目标**：让 Reviewer/Fixer 经 `mcporter` 读取真实 GitHub PR 与源码，且 Worker 不持有 GitHub 凭证（与 quickstart Step 7「Higress 托管 MCP、PAT 集中保管、Worker 用 mcporter」的架构目标一致）。

**遇到的问题**：官方 `setup-mcp-server.sh` / 安装器 `setup-higress.sh` 通过 `PUT /v1/mcpServer` 把工具定义（`rawConfigurations` + `accessToken`）写入 Higress。在本机 v1.1.2 环境中，该 PUT 返回 200 并回显配置，但 `rawConfigurations` 不持久化（GET 回空），mcp-server 插件拿不到工具/凭证，请求被透传到 `api.github.com` 返回 400。安装器与 setup 脚本使用同一段写入代码，因此重装未必能解决。

**采用的方案（凭证隔离桥）**：自建 `github-mcp-bridge` 镜像，以 `mcp-proxy` 把 GitHub 官方 MCP server（stdio）桥接为网络 SSE 服务，**GitHub PAT 仅存在于桥容器 env**（经 `--pass-environment` 转发给 stdio 子进程）。Worker 经 `mcporter` 连 `http://github-mcp:8082/sse`，**不持有任何 GitHub 凭证**——保住了「Worker 零凭证」的安全属性。该结论限定于当前本地环境与版本。

**验证**：Worker 经 MCP 读到 `nghqqa/mergepilot-test` 仓库 `feature/vulnerable-pr` 分支的真实代码（SQLi + 硬编码密钥），与 `sast-scan` 检测点对齐；并已实测完整写链路 —— 建修复分支 `fix/security-hardening`、写入修复版 `user_service.py`、提修复 [PR #2](https://github.com/nghqqa/mergepilot-test/pull/2) 并回读校验修复内容。44 个 GitHub 工具（读 PR、建分支、提 PR、合并等）可用。

---

## 3. 自主编排与任务隔离（2026-07-25，已验证）

**① handoff 零-nudge（确定性 watcher）**：Manager 的 LLM 编排不可靠（即便 SOUL 有显式状态机，review 后仍停）。改用**确定性 handoff watcher**（`tools/handoff_watcher_v2.py`）——常驻 manager 容器，动态发现 Matrix 房间，检测 `TASK_COMPLETED` 后向下一阶段 worker 发**真 @mention** 驱动（经实测：worker 只认真 mention 胶囊，不认纯文本 @）。已在干净环境端到端验证：一次提交 → review→fix→verify→裁定，全程零人工 nudge。

**② per-task room 任务隔离**：每个 PR 建专属 Matrix 任务房间（`tools/submit_pr_taskroom.py`）→ OpenClaw 按 Matrix 房间隔离 session（session key = `agent:main:matrix:channel:<room_id>`，实测）→ 零跨-PR 上下文污染。已验证全链路在单个隔离任务房间内跑通，不再需要重创 worker。

**关键工程结论**：① LLM 编排本质不可靠，确定性 watcher 是正解（不是 SOUL 状态机）；② OpenClaw session 按房间隔离是框架白拿的能力，per-task room 设计天然解决上下文串味；③ worker 只响应真 @mention 胶囊（`formatted_body` + `m.mentions`）。

> 这些工程结论随后被正式化为 M3 的确定性 Workflow Controller（PG 权威状态 + Outbox 幂等派发），见 [`项目状态.md`](项目状态.md) 与 [`复赛路线图.md`](复赛路线图.md) 的 M3-A 章节。

---

## 4. 手动逐步旧路径（参考）

> 这是一条面向早期 HiClaw v1.1.2 环境的手动 Demo 路径，**不是当前推荐入口**。
> 当前可复现路径见 [README 快速开始](../README.md#快速开始)；环境搭建见 [`环境搭建-HiClaw-WSL.md`](环境搭建-HiClaw-WSL.md)。

```bash
# 原 Team 路径（fixture PR）：见 原型搭建-Team重建.md

# 全 OpenClaw 路径（Manager 编排）：
MSYS_NO_PATHCONV=1 wsl -- bash -c 'docker cp tools/submit_manager_orchestrate.py hiclaw-manager:/tmp/ && \
  docker exec hiclaw-manager python3 /tmp/submit_manager_orchestrate.py <admin_password>'

# 汇总 Trace 与看板：
python tools/trace_aggregator.py
python tools/make_dashboard.py
python tools/audit_trail.py
```

早期一键 Demo（任务房间版）：

```bash
# 一条命令：起 GitHub MCP 桥 → 配置 worker → 起 watcher → 建任务房间 + 发审查任务
MSYS_NO_PATHCONV=1 wsl -- bash tools/demo.sh <branch> <pr_number> [prefix]
# 例：
MSYS_NO_PATHCONV=1 wsl -- bash tools/demo.sh feature/m1-e2e 6 demo-pr6

# 观察（从 demo.sh 输出取 room_id）：
MSYS_NO_PATHCONV=1 wsl -- bash tools/run-room-recent.sh <room_id> 10
# watcher 日志：
docker exec hiclaw-manager tail -f /tmp/watcher_v2.log
```

该路径依赖一个已存在的本地 HiClaw（Manager + 3 Worker）+ DeepSeek 环境，且 `hiclaw_live=false`，仅适用于隔离/候选场景。

---

## 5. SAST 两条路径（必须区分）

仓库内存在两套 SAST 实现，**任何"已验证属性"都只对应其中一套，不得互相外推**。

### A. M4-F 正式 Skill DAG 路径（新版，契约化）

- skill name：`sast-scan`
- Python 模块：`skills.sast_scan.run`
- worker mapping：`tools/m4f_skill_worker.py`（六 Skill DAG 之一）
- 覆盖测试：M4-C 的 **87 项确定性测试**（含 secret fail-closed、deadline、组件级路径安全、trusted-config、网络隔离、cleanup fail-closed）
- 拥有：JSON Schema 输入/输出、统一 envelope、凭证脱敏、1 MiB 上限、退出码分类

### B. 早期 Reviewer/demo 兼容路径（旧版）

- 入口：`skills/sast-scan/scan.py`（单文件、137 行、直接 stdout JSON）
- 当前 `config/souls/reviewer/SOUL.md` 仍引用它
- 早期 PR #1/#3 demo 使用该类路径
- **没有**契约、schema、fail-closed、1 MiB 限制、凭证脱敏等加固属性

**统一两套实现（让 SOUL/Reviewer 切到新版）列为初赛后技术债，本轮不做。** 上述新版属性**不得外推**到旧版；旧版**没有 87 项测试**。
