# PRODUCTIZATION_STAGING_AND_RELEASE_PREPARATION — 报告

基线：7a1c0ed（未回退） · 2026-09-26

## 十一项核验结果

| # | 项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 隔离持久 staging | ✓ | promote-console/pg/minio（unless-stopped） |
| 2 | Fixer/Verifier 仅隔离 | ✓ | fixture/clone 模式，零生产容器 |
| 3 | 全链回归 | ✓ | ops-monitor 19/19 |
| 4 | 真实 PR #426/#2 只读 | ✓ | PASSED 2、PG 4\|0\|2 |
| 5 | PR #16 OPEN | ✓ | head f950074、not merged |
| 6 | 不可变 GHCR tag | ✓ | v0.1.0-rc1 = rc-20260926 = 1056df76 |
| 7 | health/认证/allowlist/重启/回滚 | ✓ | health 200 + 重启恢复 + PG 持久 |
| 8 | A 链 reference-only | ✓ | ok、hits=2 |
| 9 | C 链 BLOCKED | ✓ | 三缺口不变 |
| 10 | 报告/手册/反馈 | ✓ | 本文档 + 支持手册 |
| 11 | 官网预览 | ✓ | 内容已备、不公开 |

## GHCR 镜像

| Tag | Digest | 状态 |
|---|---|---|
| rc-20260926 | sha256:1056df76... | ✓ |
| **v0.1.0-rc1** | sha256:1056df76... | ✓（不可变 tag，digest 一致） |

## 支持手册摘要

### 启动
```bash
cd distribution/docker && cp .env.example .env
# 填入密钥 → docker compose up -d → http://127.0.0.1:4730
```

### 停止/回滚
```bash
docker compose down           # 停止（保留数据）
docker compose down -v       # 停止+清数据
docker tag ...:rollback-prev ...:candidate  # 回滚镜像
```

### 监控
```bash
curl http://127.0.0.1:4730/api/health
node verification/gate/ops-monitor.mjs    # 19 项检查
```

### A 链开关
- ON: env `MERGEPILOT_ORG_RAG_A_CHAIN=1`
- OFF: 不设此 env → `a_chain_disabled`

## 用户反馈清单（截至 2026-09-26）

| ID | 描述 | 严重度 | 状态 |
|---|---|---|---|
| UA-FB-01 | 移动端 ESC 键（合成事件假象，真实按键正常） | P3 | 已解 |
| UA-FB-02 | source 在折叠 details 内（可选加 LIVE 徽章） | P4 | 待优化 |
| FG-FB-01 | staging 密码硬编码于测试脚本 | P3 | 待改 env var |
| GH-LIMIT | GitHub 自我审批限制（PR #16） | 平台 | 记录在案 |

## 官网预览（内容就绪，不发布）

- 产品定位：安全审查工作台
- 核心能力：Review Agent / Fixer / Verifier / Console / A 链
- 文档：16 篇 + Quickstart + API 合同
- 限制：C 链 BLOCKED / GitHub 只读 / 不自动 merge

## 判定

**PRODUCTIZATION_STAGING_VERIFIED**
