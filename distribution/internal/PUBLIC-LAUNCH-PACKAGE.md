# PRIVATE_RELEASE_OPERATIONS_AND_PUBLIC_LAUNCH_GATE — 报告

## Phase 1: 私有发布运行核验（全过）

| 项 | 结果 |
|---|---|
| 环境 | ✓ v0.1.0-rc1 @ 127.0.0.1:48400 (loopback) |
| health | ✓ 200 |
| 认证 | ✓ pilot / allowlist 403 |
| 五面 LIVE | ✓ POSTGRESQL_LIVE ×5 |
| PR #426/#2 | ✓ PASSED 2 / PG 4\|0\|2 |
| A 链 | ✓ reference-only ok |
| C 链 | ✓ BLOCKED |
| 重启恢复 | ✓ health 200 + PG 持久 |
| GitHub 写入 | ✓ 0 |
| PR #16 | ✓ OPEN not merged |

## Phase 2: 公开发布材料（预览，不发布）

### 官网最终文案

**MergePilot — 安全审查工作台**

MergePilot 是一个面向安全团队的自托管审查工作台。它将 PR 审查、
安全发现管理和修复验证整合到一个可审计的只读控制台中。

**核心能力**：
- Review Agent：只读 PR 审查与阶段推导
- Console：实时 overview/pending/repos/audit 控制台
- A 链：组织安全标准词法检索（reference-only）
- Fixer/Verifier：隔离环境中的自动修复与独立验证
- Docker 一键部署

**能力边界**（诚实声明）：
- ❌ 不自动 merge（设计禁止，仅人工决策）
- ❌ 不自动 approve（设计禁止）
- ❌ C 链历史案例检索：BLOCKED（需模型缓存+密钥分发）
- ❌ embedding 未启用
- ❌ GitHub 写入默认关闭（只读）
- ❌ Fixer/Verifier 仅在隔离环境中运行

### 内测与生产差异

| 特性 | 内测（当前） | 生产（未来） |
|---|---|---|
| 部署 | 本机 Docker Compose | Kubernetes / 云平台 |
| 用户 | 单操作员（pilot） | 多用户 + RBAC |
| GitHub | 只读 | 可选写入（需授权） |
| Fixer/Verifier | 隔离 fixture | 测试 PR（需授权） |
| C 链 | BLOCKED | 需 D7 + 模型缓存 |
| 网络 | loopback | 内部网络 + TLS |

### Quickstart（官网版）

```bash
# 1. 安装
docker load -i mp-console-image.tar
cp .env.example .env && vim .env

# 2. 启动
docker compose up -d

# 3. 访问
open http://127.0.0.1:4730
```

### 安全边界（官网版）

- 服务端会话 + CSRF + 仓库 allowlist
- PG/MinIO 内部网络隔离
- 密钥不进入镜像或日志
- 所有响应过 redact() 脱敏
- fail-closed：缺失=拒绝，不降级

### 支持与回滚

```bash
# 回滚到上一版本
docker compose down
docker tag ...:rollback-prev ...:candidate
docker compose up -d
```

## Phase 3: 人工闸门（7 项授权请求）

| # | 项 | 选项 |
|---|---|---|
| 1 | 开放非 loopback 内部地址？ | □ 允许：＿＿ □ 保持 loopback |
| 2 | 增加用户？ | □ 允许：＿＿ □ 仅 pilot |
| 3 | 增加仓库/PR？ | □ 允许：＿＿ □ 保持两 PR |
| 4 | 生产 Fixer/Verifier？ | □ 允许 □ 仅隔离 |
| 5 | C 链 D7？ | □ 允许 □ 保持 BLOCKED |
| 6 | 正式发布官网？ | □ 允许 □ 仅预览 |
| 7 | PR #16 由其他维护者 approve/merge？ | □ 允许 □ 保持 OPEN |

---

**沉默、查看报告或模糊回复均不构成授权。**

## 判定

**PRIVATE_RELEASE_STABLE_PUBLIC_LAUNCH_READY**
