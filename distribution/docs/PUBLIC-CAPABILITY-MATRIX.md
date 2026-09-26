# MergePilot v0.1.0 — 公开能力矩阵

## ✅ 可用能力

| 能力 | 状态 | 说明 |
|---|---|---|
| Review Agent | ✅ 可用 | 只读 PR 审查 + 阶段推导（七阶段权威枚举） |
| Console | ✅ 可用 | 实时控制台（overview/pending/repos/PR/audit） |
| Session + Allowlist | ✅ 可用 | 服务端会话 + CSRF + 仓库 allowlist |
| A 链组织知识检索 | ✅ 可用 | 词法检索（reference-only，不参与风险决策） |
| Docker 部署 | ✅ 可用 | Compose 模板 + .env.example + 镜像 |

## ⚙️ 受控能力

| 能力 | 状态 | 说明 |
|---|---|---|
| Fixer | ⚙️ 受控 | 隔离环境中的自动修复（隔离 clone/fixture） |
| Verifier | ⚙️ 受控 | 独立验证（不接受 Fixer reasoning） |
| Fixer→Verifier 联调 | ⚙️ 受控 | 隔离环境中的完整闭环 |

## ❌ 不可用 / 设计禁止

| 能力 | 状态 | 原因 |
|---|---|---|
| 自动 merge | ❌ 禁止 | 设计决定（所有合并须人工） |
| 自动 approve | ❌ 禁止 | 设计决定 |
| C 链（历史案例检索） | ❌ BLOCKED | 需 model cache + metadata attest + key distribution |
| embedding | ❌ 未启用 | 需 D7 授权 |
| pgvector | ❌ 未启用 | 依赖 embedding |
| RUN_BINDING_AUTH | ❌ NOT_WIRED | 密钥分发未闭合 |
| GitHub 写入 | ❌ 默认关闭 | 只读设计 |
| 全量生产自动修复 | ❌ 未启用 | 需生产授权 |

## 版本

- 镜像：`ghcr.io/<your-org>/mergepilot-console:v0.1.0`
- Digest：`sha256:1056df76...`
- Trivy：0 漏洞
- SBOM：CycloneDX（51KB）
