# 第三方依赖、商业服务与数据边界

> 核对日期：2026-07-31。初赛包仅包含可复现原型与证据；复赛部署前应固定容器 digest、工具版本或 Git SHA，并重新检查许可证、价格与安全公告。

| 依赖 / 服务 | 当前用途 | 状态 | 边界与替代方案 |
|---|---|---|---|
| AgentTeams（HiClaw）v1.1.2 本地环境 | Manager/Worker、Matrix、MinIO、Worker 隔离 | 已验证 | 赛题基点；当前环境 Manager 使用 OpenClaw 更稳定，该结论不外推到所有版本 |
| Higress | 模型侧网关与 consumer-token | 随框架验证 | 可替换自建网关；迁移成本高 |
| DeepSeek API `deepseek-v4-flash` | Agent 推理 | 已验证 | 商业 API；2026-07-23 查阅价：缓存未命中输入 $0.14/百万 Token、输出 $0.28/百万 Token，以官方实时价格为准；可替换百炼兼容模型 |
| GitHub 官方 MCP server | 读取仓库/PR，建分支、写文件、提 PR、合并 | 已验证 | 仅访问团队授权仓库；可替换 GitLab MCP/API；正式部署应固定镜像 digest |
| `mcp-proxy>=0.3.1` + mcporter | 将 stdio MCP 暴露为 sidecar 网络服务并供 Worker 调用 | 已验证 | GitHub PAT 仅在 sidecar；当前 Dockerfile 使用版本范围，复赛前需锁版本 |
| Python | SAST、证据工具、audit/RAG 脚本 | 已验证 | `sast-scan` 使用标准库；RAG 另有锁定依赖 |
| `fastembed==0.7.4` | 384 维 embedding | 已验证 | 模型为 `BAAI/bge-small-en-v1.5`；中文生产场景可换中文优化模型 |
| `psycopg2-binary==2.9.12` | RAG 连接 PostgreSQL/pgvector | 已验证 | 可改用 psycopg3；连接参数均支持环境变量 |
| `pgvector/pgvector:pg16` | 本地结构化审计 + 向量检索 | 已验证 | 当前不是 PolarDB 云实例；兼容迁移至 PolarDB-PG，正式环境应锁 image digest、使用密钥管理 |
| Nacos | Agent/Skill/Prompt 治理 | 规划 | 可替换 Consul/etcd |
| RocketMQ | PR 事件与可靠通知 | 规划 | 可替换 Kafka/RabbitMQ |
| AgentLoop / OpenTelemetry | 实时 Agent/Skill/MCP/LLM Trace 与 Metrics | 规划 | 当前已有本地 Trace + PG audit events；可替换 LoongSuite + AgentScope |
| `alibabacloud-sls-query` + SLS | 按 TraceId 查询云日志 | 规划 | 阿里云官方用云 Skill；不可达时降级读取 CI 产物日志 |

## 权限与数据处理

- 仅处理团队明确授权的仓库、PR、CI 产物与日志，不跨租户检索。
- Worker 不持 GitHub PAT、模型 Key 或云 AccessKey；GitHub PAT 仅在 MCP sidecar，模型 Key 由网关/环境管理。
- findings、Trace、日志和复盘报告中的 AccessKey、Token、Cookie、私钥必须脱敏。
- 高风险写操作保留人工审批；写调用应检查现状并记录 SHA、via 与审计事件。
- 演示用数据库密码通过环境变量 `PG_PASS` / `PG_PASSWORD`(或整串 `PG_DSN`)注入,脚本不写死密码;`sk-live-...` 仅为确定性扫描 fixture,不是真实密钥。

## M4-A 公共 Skill runtime 依赖（核对日期 2026-07-29）

在仓库外隔离 venv 中实际安装并跑通全部 M4-A 测试后固定。许可证逐项核对自 site-packages
内 dist-info 的许可证文件与元数据（非报告推断）。

| 依赖 | 固定版本 | 用途 | 许可证 | 许可证来源位置 | 传递依赖（pip 自动解析） |
|---|---|---|---|---|---|
| `jsonschema` | 4.25.1 | runtime：Skill Contract JSON Schema 校验 | MIT | `jsonschema-4.25.1.dist-info/licenses/COPYING`；METADATA `License-Expression: MIT` | attrs, jsonschema-specifications, referencing, rpds-py |
| `pytest` | 8.4.2 | dev/test：契约测试运行器 | MIT | `pytest-8.4.2.dist-info/licenses/LICENSE`；Classifier `License :: OSI Approved :: MIT License` | colorama, exceptiongroup, iniconfig, packaging, pluggy, pygments, tomli |

- runtime 清单：`skills/common/requirements.txt`（仅 `jsonschema==4.25.1`）。
- dev/test 清单：`skills/common/requirements-dev.txt`（`-r requirements.txt` + `pytest==8.4.2`）。
- 隔离 venv 完整 freeze（含全部传递依赖）记录在 `evidence/m4/m4a/verification.txt`。
- 不使用 `>=`；所列为实际安装并验证通过的确切版本。

## M4-C TestRunner 生产容器镜像（核对日期 2026-07-31）

镜像 `mergepilot/test-runner-py` 经 `skills/test_runner/Dockerfile` 构建。Dockerfile 默认固定基础镜像索引 digest，仍允许 deploy 通过 `PYTHON_BASE` 显式覆盖。

| 层 | 内容 | 版本 | 许可证 |
|---|---|---|---|
| 基础镜像 | `python:3.9.25-slim` | index `sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b`；linux/amd64 manifest `sha256:dad5b29e3506c35e0fd222736f4d4ef25d21b219acdd73f7bb41d59996ca8e0d` | PSF-2.0 (Python)；Debian 组件遵循各自许可证 |
| 容器内生产工具 | `pytest` | 8.4.2（容器内生产工具，非仅 dev） | MIT |

### 镜像 digest

- Registry digest：`sha256:41c6ab6e8dd9a8dcacfad34650df2aa12079ddb6fd844fdaa778d6c5ba7376b0`
- 生产运行时由 deploy 经 `MERGEPILOT_TR_IMAGE=repository@sha256:<digest>` 提供。
- 可复现构建：`docker build -t localhost:5000/mergepilot/test-runner-py:1.0.0 -f skills/test_runner/Dockerfile .`
- 结构化构建证据：`evidence/m4/m4c/image-build.json`；四场景生产链证据：`evidence/m4/m4c/container-e2e.json`。

### 容器 Python 包清单

容器内 `python -m pip freeze`（镜像 digest 如上）：

| 包 | 版本 | 许可证 |
|---|---:|---|
| `pytest` | 8.4.2 | MIT |
| `exceptiongroup` | 1.3.1 | MIT |
| `iniconfig` | 2.1.0 | MIT |
| `packaging` | 26.2 | Apache-2.0 OR BSD-2-Clause |
| `pluggy` | 1.6.0 | MIT |
| `Pygments` | 2.20.0 | BSD-2-Clause |
| `tomli` | 2.4.1 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |

基础镜像自带 `pip==23.0.1`、`setuptools==79.0.1`、`wheel==0.45.1`；其许可证随 Python 官方基础镜像发行材料核对。

### 离线依赖漏洞 advisory 数据

- 来源：MergePilot 离线本地 advisory 集（人工整理自 OSV/CVE 公开数据）
- 版本：`2026-07-advisory-snapshot`
- 覆盖：pypi
- 数据许可证：CVE 数据为公共领域；OSV 数据遵循 CC-BY 4.0
- valid_until：2027-01-30

## M4-D PRLifecycle 生产 adapter（核对日期 2026-07-31）

`skills/pr_lifecycle/requirements.txt` 仅为 Policy Gateway MCP 生产 adapter
提供精确依赖；框架中立 core 与 M4-A/B/C 回归仍使用既有 Python 3.9 环境。
生产 adapter 需要 Python 3.10+，本轮在 Python 3.13.14 和真实 fixture E2E
runner Python 3.12.13 中验证。

| 依赖 | 精确版本 | 用途 | 许可证 |
|---|---:|---|---|
| `mcp` | 1.28.1 | MCP SSE client/session，连接 Policy Gateway | MIT |
| `httpx` | 0.28.1 | MCP HTTP transport；adapter 固定 `trust_env=false`、不跟随重定向 | BSD-3-Clause |
| `anyio` | 4.14.2 | MCP SDK 异步运行时依赖 | MIT |

版本权威清单：`skills/pr_lifecycle/requirements.txt`。不使用 `>=`；M4-D
verification 会校验 exact pins 与实际安装版本一致。
