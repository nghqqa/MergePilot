# DEPLOY — 部署与使用指南

四种使用方式按投入递增。所有方式共享同一组真实性边界：RAG 语料 SYNTHETIC/REDACTED、
Database Branch 为 SIMULATED 隔离模拟、PolarDB NOT CONNECTED、PR Auto Merge DISABLED；
本项目未声称生产部署验证。

---

## A · 无 Docker：演示平台快速体验（推荐首选）

零依赖、离线可用，约 2 分钟。

```bash
git clone https://github.com/nghqqa/MergePilot.git
cd MergePilot/demo-platform
node backend/server.mjs        # 打开 http://127.0.0.1:4173
```

- 前端已预构建（`frontend/dist/`），无需 `npm install`、无需网络
- 页面为历史已验证运行的回放（REPLAY 模式），不会访问真实 GitHub，不会产生写操作
- 自测：`node backend/test/selftest.mjs` → 54 passed, 0 failed
- 不想 clone：下载 [MergePilot-demo.zip](https://github.com/nghqqa/MergePilot/releases/download/v0.2.0/MergePilot-demo.zip)

**完成标志**：控制台三条安全路径页面可打开，`/api/health` 返回 `status: ok`。

---

## B · 离线镜像加载（完整隔离栈）

适用于需要运行**完整隔离栈**（Controller / Policy Gateway / PostgreSQL/pgvector /
gh-webhook / demo-console / console-edge / preflight，以及可选 gh-proxy / mcp-bridge
真实 GitHub 链路服务）的环境。离线、无需构建、无需外部镜像仓库。

### 下载

从 [runtime-images-20260912 Release](https://github.com/nghqqa/MergePilot/releases/tag/runtime-images-20260912) 下载：

| 文件 | 说明 |
| --- | --- |
| `MergePilot-images-linux-amd64.tar.zst` | 9 个镜像的 zstd 压缩 docker save 归档 |
| `MergePilot-images-manifest.json` | 镜像 / tag / digest / 架构 / source_commit 清单 |
| `SHA256SUMS` | 资产校验和 |

先核对：`sha256sum -c SHA256SUMS`。

### 加载

WSL2 / Linux：

```bash
./release/images/load-images.sh <下载目录>
```

Windows PowerShell（Docker Desktop）：

```powershell
.elease\images\load-images.ps1
```

脚本会：解包 → `docker load` → 逐镜像校验**数量 / tag / digest / 架构**，
全部一致输出 `VERIFY_OK: 9/9`。

### 启动隔离栈

```bash
mergepilot doctor                 # 环境体检
mergepilot install                # 记录镜像 ID、生成 secret env 文件
mergepilot start                  # 按契约启动隔离栈（preflight 门禁全绿后对外）
```

说明：`postgres.env` / `controller.env` / `gh_webhook.env` / `demo_console.env`
等 secret 文件由编排器在 install/start 时**本地生成**——它们从不进入镜像或仓库。

**完成标志**：`mergepilot status` 全 healthy；demo-console 的
`/api/live/status` 返回 200 + `POSTGRES_ISOLATED` + `source_read_only`。

---

## C · 联网 pull 镜像（GHCR，预留）

当前版本的镜像**未发布到 GHCR**；`pgvector/pgvector:pg16` 是栈中唯一按 digest
拉取的上游镜像。GHCR 命名空间（`ghcr.io/nghqqa/mergepilot-isolated-*`）为预留位：
发布后本节将给出逐镜像 `docker pull ghcr.io/...@sha256:...` 命令，digest 以
`MergePilot-images-manifest.json` 为准。当前请使用方式 B。

---

## D · GitHub App / GitHub MCP 真实接入（指向你自己的仓库）

让 MergePilot 对**你自己的 GitHub 仓库**的真实 PR 执行 审查 →（高危人工门）→ 修复 → 验证。

前提：完成 B（隔离栈）+ CoPaw worker 运行时镜像（同 Release 或自建 AgentTeams 环境，
runtime 任选 CoPaw / QwenPaw / openclaw）。逐步操作见
[docs/real-loop-your-repo.md](docs/real-loop-your-repo.md)（E2E 模式 20 键配置：
`GITHUB_INGRESS_ENABLED=1`、`GITHUB_ROOM_MAP`、`GITHUB_POLICY_PATH`、Matrix、
`GATEWAY_URL`/`COORDINATOR_TOKEN`、PG 连接、`ADMIN_PW`）。

**权限与 secrets 原则**：GitHub PAT 只注入 github-mcp（网络隔离）；coordinator 只读；
fixer 只写 `fix/*` 分支、永不 main、永不自动合并；一切 secret 走编排器生成的
env 文件，不进镜像、不进 compose、不进仓库。

**诚实状态**：该链路于 2026-08 在我们的隔离栈 + e2e-fixture 仓库完成 10/10 真实
GitHub 验证（M5-0C）；你的环境按同一 20 键合同配置，未预先验证。

---

## 故障诊断

| 症状 | 原因与处置 |
| --- | --- |
| `docker version` 报 cannot connect | **Docker 未运行**：启动 Docker Desktop，等待引擎就绪（鲸鱼图标稳定） |
| Docker Desktop 报 WSL2 engine 错误 | **WSL2 未启用/未更新**：管理员运行 `wsl --update`；`wsl --set-default-version 2`；重启 Docker Desktop |
| `exec format error` / 架构不匹配 | **架构不匹配**：本包为 linux/amd64；ARM/Mac 引擎无法加载，换 linux/amd64 引擎 |
| load 后 `docker images` 缺镜像 | **镜像缺失**：重新执行 load-images 脚本；确认 SHA256SUMS 校验通过后再加载 |
| digest 校验 PROBLEM: digest 不一致 | **digest 不一致**：资产被篡改或下载不完整——重新下载并核对 `SHA256SUMS` |
| 启动报 secret env 文件缺失 | **secret 文件缺失**：先运行 `mergepilot install`（或 start）——orchestrator 会本地生成 `postgres.env` 等文件；它们从不随包分发 |
| `CONFIG_INVALID`（E2E 模式） | 20 键配置不合法：按 `tools/cli/e2e_foundation.py` 的提示修正键值 / 只读路径 |
| zstd 未找到 | Ubuntu/WSL：`sudo apt-get install -y zstd`；Windows：使用系统自带 `tar.exe` 或安装 zstd |

## 边界（不可协商）

- 网关授权（consumer-token 隔离）、最小权限、fail-closed、secret env 文件机制、
  证据完整性设计不随部署方式改变
- 默认启动不访问真实 GitHub API、不执行任何 GitHub 写操作；真实接入仅经 D 的
  显式配置启用
