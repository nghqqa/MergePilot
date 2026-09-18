# 真实闭环：指向你自己的仓库（L4）

前置：先完成 [agent-runtime-adoption.md](agent-runtime-adoption.md) 的 **L3**（你自己的
AgentTeams 环境 + CoPaw worker 镜像，见 [runtime-images-20260912 Release](https://github.com/nghqqa/MergePilot/releases/tag/runtime-images-20260912)），
并部署隔离栈（`mergepilot install` / `mergepilot doctor`，见 [mergepilot-cli.md](mergepilot-cli.md)）。

目标：让 MergePilot 对**你自己的 GitHub 仓库**的真实 PR 执行
审查 →（高危人工门）→ 修复 → 验证，证据落 PostgreSQL / MinIO。

> **诚实状态**：这条链路在 2026-08 于我们的隔离栈 + `nghqqa/MergePilot-e2e-fixture`
> 仓库完成 **10/10 真实 GitHub 链路验证**（M5-0C）。你的环境按同一份 20 键合同配置，
> **未预先验证**——遇到 `CONFIG_INVALID` 先核对键值与只读路径，再跑 `mergepilot doctor`。

---

## 一、准备

| 需要 | 说明 |
| --- | --- |
| 你的 GitHub 仓库 | PR 将开在这里；仓库会进入 policy allowlist |
| GitHub PAT | 只注入 github-mcp（`mcp-backend-net` 网络隔离），不进 Worker |
| Matrix 房间 | 已存在，且 reviewer / fixer / verifier / manager 四个参与者**均已加入**（系统不自动建房） |
| LLM 网关 | `GATEWAY_URL` + `COORDINATOR_TOKEN`（Higress 或自建，见 THIRD_PARTY.md 替代方案） |
| PostgreSQL | `PG_HOST / PG_PORT / PG_DATABASE / PG_USER / PG_PASS` |

## 二、配置（E2E 模式 20 键，启动时严格校验）

校验器：`tools/cli/e2e_foundation.py`（缺键 / 未知键 / 路径穿越一律 `CONFIG_INVALID`）。

核心键：

| 键 | 值 |
| --- | --- |
| `GITHUB_INGRESS_ENABLED` | 必须恰为 `1` |
| `GITHUB_ROOM_MAP` | 只读路径，指向 `.mergepilot/room-map.yaml`（见下） |
| `GITHUB_POLICY_PATH` | 只读路径，指向 policy yaml（见下） |
| `GITHUB_DELIVERY_LEASE_SECONDS` / `GITHUB_DELIVERY_MAX_ATTEMPTS` | 投递租约与重试上限 |
| `MATRIX_HS` / `MATRIX_SERVER_NAME` / `MATRIX_USER` | Matrix homeserver / 服务器名 / 用户 |
| `CONTROLLER_CONSUMER_NAME` | Controller 在 LLM 网关的 consumer 名 |
| `RESERVED_RUN_PREFIXES` | 保留运行前缀（防写冲突） |
| `GATEWAY_URL` / `COORDINATOR_TOKEN` | LLM 网关地址与 Coordinator 令牌 |
| `PG_HOST / PG_PORT / PG_DATABASE / PG_USER / PG_PASS` | PostgreSQL 连接 |
| `ADMIN_PW` | 控制台管理口令 |

两个 YAML：

```bash
mkdir -p .mergepilot
cp config/gh-app/room-map.example.yaml .mergepilot/room-map.yaml
# 编辑 .mergepilot/room-map.yaml：填你的真实 "owner/repo" 与 Matrix room ID
cp config/m5-0c/real-github-policy.yaml .mergepilot/policy.yaml
# 编辑 .mergepilot/policy.yaml：repos.allowlist 改为你的 "owner/repo"
```

**契约（fail-closed）**：room-map 的 repo 集合必须与 policy `repos.allowlist` **1:1**
（缺失 / 多余 / 重复都会让 Controller 的 github drain 拒绝启动）；系统**不自动创建**
Matrix 房间；房间与参与者就绪前不要置 `GITHUB_INGRESS_ENABLED=1`。

角色与模型：`config/team.yaml`（Team CR：coordinator + reviewer / fixer / verifier 与模型）、
`config/souls/`（四个角色的 persona）。

## 三、启动与验证

```bash
mergepilot doctor                    # 环境体检
mergepilot --github-e2e start        # 20 键前置门 → 启动 DAG → 11 服务状态
# 在你的仓库开一个 PR……
mergepilot --github-e2e status       # 运行状态
mergepilot --github-e2e stop         # 有序停止（只停自己拥有的）
mergepilot --github-e2e cleanup      # 清理会话容器 / 网络 / secrets（保留镜像）
```

预期流转：PR 事件 → Matrix 房间出现 Reviewer 审查 →（高危）人工门暂停 →
批准后 Fixer 在 `fix/*` 分支修复（写操作只经 pr-lifecycle Skill，永不进 main、
永不自动合并）→ Verifier 探针复核 → 证据入库；coordinator 全程只读。

## 四、边界

安全状态机与审计链路零改动；数据边界四条不变
（RAG SYNTHETIC/REDACTED · Branch SIMULATED · PolarDB NOT CONNECTED ·
PR Auto Merge DISABLED）。本指南描述的是**开发预览链路**，非生产部署。
