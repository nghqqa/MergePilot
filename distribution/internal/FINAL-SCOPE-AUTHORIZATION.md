# FINAL_PRODUCT_SCOPE_AND_PUBLIC_RELEASE_GATE — 授权包

基线：b98a097 · v0.1.0 · 2026-09-26 · 14/14 确认通过

## 授权包 A：公开开源发布

| # | 项 | 选项 |
|---|---|---|
| A1 | Push 源码仓库到 GitHub public？ | □ 允许：＿＿ □ 不允许 |
| A2 | 发布 v0.1.0 GitHub Release？ | □ 允许 □ 不允许 |
| A3 | 发布 distribution/{docs,docker,website}？ | □ 允许 □ 不允许 |
| A4 | 托管方式？ | □ GitHub Pages □ Netlify □ 自托管 □ 暂不 |
| A5 | 域名？ | ＿＿＿＿ |
| A6 | HTTPS？ | □ Let's Encrypt □ 商业证书 □ 暂不 |

## 授权包 B：生产 Fixer/Verifier

| # | 项 | 选项 |
|---|---|---|
| B1 | 允许的用户？ | □ 仅 pilot □ 增加：＿＿ |
| B2 | 允许的仓库/分支？ | □ fastapi-boilerplate-demo/test-* □ 增加：＿＿ |
| B3 | 允许真实 push 修复？ | □ 允许（仅测试分支） □ 不允许 |
| B4 | 允许人工 approve？ | □ 允许（第二维护者：＿＿） □ 不允许 |
| B5 | 允许人工 merge？ | □ 允许 □ 不允许 |
| B6 | Rollback？ | git revert + 分支删除 + PR 关闭 |
| B7 | 有效期？ | □ 7 天 □ 30 天 □ 自定义：＿＿ |

## 授权包 C：C 链

| # | 项 | 选项 |
|---|---|---|
| C1 | 允许 D7 下载？ | □ 允许（模型源：＿＿） □ 保持 BLOCKED |
| C2 | Approved model cache？ | □ 提供（model/version/dimension/SHA256） □ 不提供 |
| C3 | Provider metadata attest？ | □ 提供 □ 不提供 |
| C4 | RUN_BINDING_AUTH 密钥分发？ | □ 建立 □ 保持 NOT_WIRED |
| C5 | embedding/pgvector 启用？ | □ 启用 □ 保持 DISABLED |

---

## 判定

**FINAL_PRODUCT_SCOPE_AUTHORIZATION_READY**

**沉默、查看报告或模糊回复均不构成授权。**
