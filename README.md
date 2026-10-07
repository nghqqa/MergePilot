# MergePilot

**自托管的多 Agent PR 审查工作台。审查留痕，合并由维护者决定。**

[官网](https://docs.nghqqa.cn/) · [快速开始](https://docs.nghqqa.cn/quickstart.html) · [使用文档](https://docs.nghqqa.cn/docs.html) · [下载与校验](https://docs.nghqqa.cn/downloads.html)

[![Release](https://img.shields.io/badge/Release-rc.17-0e6b62)](https://github.com/nghqqa/MergePilot/releases/tag/v0.2.0-beta.6-rc.17)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

MergePilot 通过 GitHub App 只读接入仓库，把 PR 事件、审查发现、修复建议与验证证据汇集到你自己的 Console。
它不回写 GitHub Check Run，不自动合并或批准 PR；当前为 **Developer Beta（rc.17 预发布）**。

## 界面

![MergePilot Console：PR 列表、风险状态与审查进度](docs/assets/readme/console-overview-rc16.png)

*rc.16 测试环境的真实历史数据；账号、组织与角色信息已打码。阻断与异常为演练状态，不代表所有部署的运行情况。*

## 它如何工作

PR 事件接入 → 审查发现 → 按策略进入修复与验证 → Console 展示结果。

四个 Agent 角色各司其职，不代表四个 LLM，也不代表每个 PR 都执行全部环节：

| 角色 | 职责 |
| --- | --- |
| Reviewer | 审查代码，产出发现与建议 |
| Fixer | 生成修复建议，默认 dry-run |
| Verifier | 独立验证，记录判定与证据 |
| Leader | 编排与裁决；内部确定性策略保留管线终裁权 |

内部 Leader 决定的是管线状态，**维护者决定的是是否合并**。高危 finding 的修复需人工审批；
Agent 的建议与可执行验证证据应分开看待。

- **团队协作**：GitHub OAuth + 邀请制登录、五角色 RBAC、应用层多租户隔离与审计。
- **修复验证**：AgentTeams 执行器独立部署；未配置或不就绪时，该环节 fail-closed。
- **可选知识库**：本地 RAG 检索团队规范，结果仅作为参考，不自动成为审查结论。

## 开始使用

Console、数据库和部署配置由你自行管理；官网是文档入口，**不是托管版 Console 或登录入口**。

1. 阅读[快速开始](https://docs.nghqqa.cn/quickstart.html)，准备 Docker Compose、Node.js ≥ 20、GitHub App、OAuth App 和审查用的 LLM 端点。
2. 选择 [rc.17 部署包](https://github.com/nghqqa/MergePilot/releases/tag/v0.2.0-beta.6-rc.17)，或使用下面的仓库部署方式。离线镜像、SHA256SUMS、SBOM 和扫描记录见[下载与校验](https://docs.nghqqa.cn/downloads.html)。
3. 按[部署指南](deploy/selfhost/README.md)完成配置和首管理员初始化，再登录你自己的 Console。

### 从仓库部署

以下使用 `main` 的部署文件；当前 compose 固定引用 rc.17 镜像，包含发布后修正的 preflight。
冻结的 rc.17 部署包不会随 `main` 自动更新。

```bash
git clone https://github.com/nghqqa/MergePilot.git
cd MergePilot/deploy/selfhost
cp .env.example .env
# 按部署指南填写 .env；Linux/macOS 下执行 chmod 600 .env
node preflight.mjs
docker compose --env-file .env up -d
node preflight.mjs --live
```

本机登录地址：`http://127.0.0.1:48500/login`。**容器健康不等于审查链路就绪**：还需核实身份初始化、
Webhook 接收、LLM 调用，以及可选的修复验证执行器。

- **首次管理员**：生产关闭 fixture 登录；首个 `platform_admin` 需部署者在数据库初始化。见[首管理员步骤](deploy/selfhost/README.md#首个平台管理员初始化生产)。
- **成员邀请**：管理员绑定成员的 GitHub 数字 ID、租户、角色与有效期；成员打开自己部署实例的 `/login?invite=<invite_id>`。无公共注册，邀请不能授予 `platform_admin`。
- **网络入口**：OAuth callback 和邀请地址需由登录者浏览器访问；`127.0.0.1` 只适用于本机。自动 Webhook 需 GitHub 服务端可达的 HTTPS 入口。Console 与 Webhook 的地址、端口和反代配置见部署指南。

## 使用前的边界

- **自托管不等于零出站**：配置外部 LLM 后，脱敏 PR 元数据、finding 摘要与相关 diff 片段会发送到你配置的端点。详见[数据出站说明](#数据出站说明)。
- **不是自动合并工具**：MergePilot 本体对 GitHub 只读，结论呈现在 Console；外部执行器应独立审查其权限和配置。
- **Beta 不含企业能力**：无 PostgreSQL RLS、SSO/SCIM、HA 或配额限流；当前多租户隔离在应用层实施。
- **扫描结果有时效**：Release 中的 SBOM 与安全扫描是该制品的发布时点记录，不构成永久无漏洞保证。

<details>
<summary>数据出站说明</summary>

### 数据出站说明

默认 `MU_LLM_PROVIDER=disabled` 时，LLM 调用不发起网络请求。显式配置 OpenAI 兼容端点后，
Reviewer 审查上下文包含脱敏 PR 元数据、最多 50 条 finding 摘要及相关 diff 片段，总载荷不超过 24KB；
常见密钥格式会被掩码，但部署者仍需审查所选服务的数据处理政策。

AgentTeams 是独立执行器，不是所有角色统一走外部 LLM。可选 cchain 用于部署自证；
`BLOCKED` 默认为状态呈现，仅显式启用 `MERGEPILOT_CCHAIN_ENFORCE=1` 时门控修复验证启动。
配置、审计和运行细节见[部署指南](deploy/selfhost/README.md)与[AgentTeams 手册](deploy/agentteams-beta/RUNBOOK.md)。

</details>

## 文档与反馈

| 要做什么 | 入口 |
| --- | --- |
| 浏览产品与文档 | [官网](https://docs.nghqqa.cn/) · [文档索引](https://docs.nghqqa.cn/docs.html) |
| 安装、配置、登录与邀请 | [自托管部署指南](deploy/selfhost/README.md) · [BETA-GUIDE](docs/BETA-GUIDE.md) |
| 部署修复验证执行器 | [AgentTeams RUNBOOK](deploy/agentteams-beta/RUNBOOK.md) |
| 配置本地知识库 | [本地 RAG 指南](distribution/docs/LOCAL-RAG-GUIDE.md) |
| 深入架构、API 与能力限制 | [技术文档目录](distribution/docs/) |
| 查看发布与变更 | [rc.17 Release](https://github.com/nghqqa/MergePilot/releases/tag/v0.2.0-beta.6-rc.17) · [CHANGELOG](CHANGELOG.md) |
| 报告问题或参与贡献 | [Issues](https://github.com/nghqqa/MergePilot/issues) · [CONTRIBUTING](CONTRIBUTING.md) |

安全问题请勿在公开 Issue 披露漏洞细节或凭据；可仅留下联系方式，由维护者接洽。
安全设计见 [SECURITY](distribution/docs/SECURITY.md)。

Apache-2.0 · [LICENSE](LICENSE)

比赛与演示时代的资料仅作历史记录，不作为当前安装或能力承诺依据。
