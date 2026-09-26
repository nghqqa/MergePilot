# POST_V0.1_INTERNAL_RELEASE — 运营报告与下一阶段授权包

版本：v0.1.0-rc1 · 基线：428fddc · 2026-09-26

## 一 · 内部版本运营核验（全绿）

| 项 | 结果 |
|---|---|
| health | ✓ 200 |
| PG 备份 | ✓ 419 行 |
| allowlist | ✓ 403 |
| 五面 | ✓ POSTGRESQL_LIVE ×5 |
| PG/MinIO | ✓ 可读 |
| PR #426/#2 | ✓ PASSED 2 / 4\|0\|2 |
| A 链 | ✓ reference-only ok |
| FXV | ✓ 仅隔离 |
| C 链 | ✓ BLOCKED |
| 镜像 | ✓ 1056df76 |

## 二 · PR #16 记录

| 项 | 值 |
|---|---|
| 状态 | OPEN |
| head | f950074 |
| reviews | 0 |
| merged | false |

结论：技术链路已验证；同账号审批受 GitHub 平台限制；如需审批须由真实第二名维护者执行；merge 是独立人工决策。

## 三 · 下一阶段授权包（10 项）

| # | 项 | 选项 |
|---|---|---|
| 1 | 第二名维护者 approve PR #16？ | □ 允许：＿＿ □ 不允许 |
| 2 | merge PR #16？ | □ 允许 □ 不允许 |
| 3 | 生产 Fixer/Verifier？ | □ 允许 □ 仅隔离 |
| 4 | 允许的仓库/分支/用户？ | □ 保持现状 □ 扩大：＿＿ |
| 5 | 增加用户？ | □ 允许：＿＿ □ 仅 pilot |
| 6 | 增加仓库/PR？ | □ 允许：＿＿ □ 保持两 PR |
| 7 | C 链 D7？ | □ 允许 □ 保持 BLOCKED |
| 8 | 提供 approved model cache？ | □ 允许（D7 前置） □ 不提供 |
| 9 | 正式发布官网？ | □ 允许（需域名+HTTPS） □ 仅预览 |
| 10 | 创建稳定版 tag？ | □ 允许 v0.1.0 □ 仅 rc1（禁 latest） |

---

**沉默、查看报告或模糊回复均不构成授权。**

## 判定

**POST_V0_1_INTERNAL_RELEASE_STABLE**

---

## v0.1.0 稳定版发布记录（2026-09-26）

| 项 | 值 |
|---|---|
| Tag | **v0.1.0** |
| Digest | sha256:1056df76... |
| 与 rc1 一致 | ✓ 逐字节相同 |
| Trivy | 0 漏洞 |
| latest tag | 未创建（禁止） |

回滚锚点：v0.1.0-rc1 · rc-20260926 · sha-157a107（全部指向同一 digest）
