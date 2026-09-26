# FINAL_PRODUCT_RELEASE_READINESS_GATE — 授权包

版本：v0.1.0-rc1 · digest：sha256:1056df76... · 基线：883fdf5 · 2026-09-26

## 十项核验结果

| # | 项 | 结果 | 详情 |
|---|---|---|---|
| 1 | 三 tag digest 一致 | ✓ | v0.1.0-rc1 = rc-20260926 = local |
| 2 | SBOM + Trivy | ✓ | 0 漏洞 |
| 3 | 跨机器导入 | ✓ | export 60MB → import digest MATCH |
| 4 | PG 备份 | ✓ | pg_dump 419 行 |
| 5 | 凭据不入日志/镜像 | ✓ | docker history 零命中 |
| 6 | 部署模板 | ✓ | compose + .env.example（零秘密） |
| 7 | 运维手册 | ✓ | 16 文档 + 本授权包 |
| 8 | PR 状态 | ✓ | #426 PASSED / #2 PASSED / #16 OPEN |
| 9 | 能力边界文案 | ✓ | 下方 |
| 10 | 授权包 | ✓ | 本文档 |

## 正式版本清单

```
镜像: ghcr.io/nghqqa/mergepilot-console
Tags:
  - v0.1.0-rc1 (不可变 semver)
  - rc-20260926 (日期 tag)
Digest: sha256:1056df767aec320e20db4db6836747c4e80becd653727447925c88cea2ad7809
SBOM: CycloneDX (51KB)
Trivy: 0 vulnerabilities (CRITICAL=0 HIGH=0)
Size: 60MB (compressed export)
Base: node:22-alpine (pinned by digest)
```

## 能力边界文案（官网用）

### ✅ 可以宣传
- 安全审查工作台（Review Workbench）
- 只读 PR 审查与阶段推导
- 实时控制台（overview/pending/repos/audit）
- 组织安全标准词法检索（A 链 reference-only）
- 隔离环境中的 Fixer→Verifier 修复验证链
- 服务端会话与仓库 allowlist
- Docker 一键部署

### ❌ 不得宣传
- ~~自动 merge~~（设计禁止，仅人工）
- ~~完整 RAG 已接入~~（C 链 BLOCKED）
- ~~生产自动修复已启用~~（Fixer/Verifier 仅隔离）
- ~~embedding 已启用~~（DISABLED）
- ~~GitHub 写入~~（只读）
- ~~自动 approve~~（设计禁止）

## 最终授权请求（6 项）

### 1. 生产部署
□ 允许部署到：＿＿＿＿
□ 不允许（保持 staging）

### 2. Fixer/Verifier 生产运行
□ 允许在持久 staging 处理测试 PR
□ 仅隔离 fixture

### 3. C 链 D7
□ 允许下载 embedding 模型
□ 保持 BLOCKED

### 4. 官网发布
□ 允许公开发布
□ 仅预览

### 5. Registry 多 tag
□ 允许推送 latest / 稳定版 tag
□ 仅 v0.1.0-rc1 + rc-20260926

### 6. PR #16 处置
□ 用其他账号 approve + merge
□ 关闭 PR + 删除分支
□ 保持 OPEN

---

**沉默、查看报告或模糊回复均不构成授权。**

## 判定

**FINAL_PRODUCT_RELEASE_AUTHORIZATION_READY**
