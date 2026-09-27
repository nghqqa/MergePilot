# MergePilot v0.1.0 — 公开能力矩阵

三层定位：**Developer**（个人开发者，预构建包即开即用）→ **Self-hosted Pilot**
（小团队自托管试点，源码自建 + 本地 RAG）→ **Enterprise Roadmap**（未实现的
路线图，面向未来有组织投入的团队）。

> 本项目当前**没有企业生产客户或企业生产接入**，也未声称 Enterprise
> Production Ready。Roadmap 列仅为方向声明，不构成交付承诺。

## 总览矩阵

| 能力 | Developer（2C2G） | Self-hosted Pilot（4C8G/8C16G） | Enterprise Roadmap |
|---|---|---|---|
| Review Agent（只读审查 + 阶段推导） | ✅ 可用 | ✅ 可用 | —（已在前两层交付） |
| Console 五面（overview/pending/repos/PR/audit） | ✅ 可用 | ✅ 可用 | — |
| Session + CSRF + 仓库 allowlist | ✅ 可用 | ✅ 可用 | — |
| 审计追踪（audit_events） | ✅ 可用 | ✅ 可用 | — |
| A 链组织知识词法检索 | ⚙️ 默认关（flag 显式开启） | ⚙️ 默认关 | — |
| 本地 RAG 语料检索（`local-hash-v1`） | ❌（发行镜像无此端点） | ✅ 试用（源码自建） | — |
| `bge-m3` 语义嵌入（自带工件 + manifest） | ❌ | ⚙️ 试用（8C16G 档） | — |
| 语料 导入/索引/删除/回滚 + 备份恢复 | ❌ | ✅（API + pg_dump 演练口径） | — |
| Fixer / Verifier（隔离 fixture） | ❌ 未启动 | ⚙️ 受控（隔离环境） | — |
| 离线部署（零外呼） | ✅（tar 导入） | ✅（pull never） | — |
| Secret Manager 集成（受控 keystore 分发） | ❌ | ❌ | 🗺️ Roadmap |
| External attestation 服务端点 | ❌ | ❌ | 🗺️ Roadmap |
| 多租户隔离（RLS / 租户模型） | ❌ | ❌ | 🗺️ Roadmap |
| C 链历史案例检索（案例库） | ❌ | ❌ | 🗫 Roadmap（3 缺口） |
| RUN_BINDING_AUTH 密钥分发 | ❌ | ❌ | 🗫 Roadmap |
| 自动 merge / 自动 approve | ❌ 设计禁止 | ❌ 设计禁止 | ❌ 长期边界（不做） |

图例：✅ 可用 · ⚙️ 受控/试用（有开关或环境前提） · ❌ 不可用 · 🗫 有明确前置缺口 ·
🗺️ 方向性路线图 · — 不适用

## Developer 层（个人开发者）

预构建发行包（`distribution/docker/`，镜像 digest `sha256:1056df76…`）。
2C2G 即可运行。能力：只读 PR 审查、阶段推导、Console 五面、审计、A 链词法
检索（默认关）。红线：不自动 merge/approve，GitHub 零写入。

## Self-hosted Pilot 层（小团队自托管试点）

源码自建（`deploy/local-rag-trial/`），在 Developer 层之上追加：

| 能力 | 状态 | 说明 |
|---|---|---|
| 本地 RAG 语料检索 | ⚙️ 试用 | 六状态诚实契约；reference-only（不自动 finding/ticket/gate/VERIFIED） |
| 嵌入策略 | ⚙️ | `local-hash-v1` 默认（零下载）；`bge-m3` 可选（自带工件，8C16G） |
| 语料生命周期 | ✅ | 导入/幂等/删除/索引失效/版本回滚（保留窗口 2） |
| 备份恢复 | ✅ | pg_dump + 临时容器恢复演练口径；MinIO 卷 tar |
| scope 门 | ✅ | `RAGTRIAL_ALLOWED_SCOPES` 默认拒绝（fail-closed） |
| Fixer/Verifier | ⚙️ 受控 | 仅隔离 fixture，不自动处理真实 PR |
| 自测 | ✅ | `node deploy/local-rag-trial/scripts/run-e2e.mjs`（26 项断言，须干净卷单跑；早期"15 项必测"为试验初期的场景数标签，已随套件扩展过时） |

红线：不宣称生产级 RAG；语义模型不自动下载；检索不参与风险决策。

## Enterprise Roadmap 层（未实现，仅方向）

| 项目 | 当前状态 | 前置缺口 |
|---|---|---|
| Secret Manager 集成 | 接口/模板/runbook 已备（`deploy/rag-prod/scripts/bootstrap-keystore.mjs`） | 受控 secret manager 选型 + 组织授权 |
| External attestation | 合同测试就绪 | 服务端点 + 组织授权 |
| 多租户隔离 | 未实现（当前为单工作台，无租户模型/RLS） | 设计 + 实现 + 验证全链 |
| C 链历史案例检索（案例库） | BLOCKED | ① 受控模型缓存通道 ② provider 元数据 attestation ③ RUN_BINDING_AUTH 密钥分发 |
| RUN_BINDING_AUTH | 接口+合同测试就绪 | 密钥生成/分发/轮换/撤销机制 |

## 长期边界（任何层级都不做）

- 自动 merge / 自动 approve（所有合并与审批由人工执行）
- 检索结果自动生成 finding/ticket/gate/VERIFIED
- 无 manifest 校验的模型加载 / 内置模型自动下载

## 版本

- 发行镜像：`mp-canonical-console:candidate`（digest `sha256:1056df76…`，随包
  `image-digest.txt`；Trivy 0 漏洞；SBOM CycloneDX）
- 本地 RAG 栈镜像：`local-rag-trial-console:local`（源码本地构建，不入发行物）
