# 第三方依赖、商业服务与数据边界

> 核对日期：2026-07-24。初赛包仅包含可复现原型与证据；复赛部署前应固定容器 digest、工具版本或 Git SHA，并重新检查许可证、价格与安全公告。

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
