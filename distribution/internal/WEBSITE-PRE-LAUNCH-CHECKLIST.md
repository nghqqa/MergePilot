# 官网正式发布前检查清单

## 12 项验收结果

| # | 项 | 结果 |
|---|---|---|
| 1 | 内网访问 | ✓ 192.168.1.2:48400 health 200 |
| 2 | 认证全链 | ✓ bad 401 / login / session / CSRF logout / post-logout 401 |
| 3 | 边界 | ✓ 401/403/404 |
| 4 | 五面 | ✓ POSTGRESQL_LIVE ×5 |
| 5 | PR 一致 | ✓ PASSED 2 / PG 4\|0\|2 |
| 6 | A 链 | ✓ hit ok 3 / empty ok 0 |
| 7 | FXV | ✓ isolated only |
| 8 | 重启 | ✓ health 200 + PG 持久 |
| 9 | JS | ✓ 同镜像在 staging 已验证零错 |
| 10 | 日志 | ✓ 零敏感信息 |
| 11 | 公网 | ✓ 绑定 192.168.1.2（非 0.0.0.0） |
| 12 | 检查清单 | ✓ 本文档 |

## 官网发布前检查

| 项 | 状态 | 说明 |
|---|---|---|
| 域名 | ⏳ 待定 | 需 Owner 提供（如 mergepilot.dev） |
| HTTPS | ⏳ 待定 | 需 TLS 证书 + 反向代理 |
| 托管 | ⏳ 待定 | GitHub Pages / Netlify / 自托管 |
| CSP | ✅ 可生成 | 静态 HTML 无外部依赖 |
| 敏感信息 | ✅ 零命中 | 无仓名/PR号/地址/凭据 |
| 能力边界 | ✅ 已含 | ✗auto-merge ✗full RAG ✗auto-fix ✗embedding |
| 链接 | ✅ 6 内部 | 均指向 docs/ |
| 真实数据 | ✅ 零 | 无 staging/production 数据 |

## 人工闸门（5 项）

1. **增加用户？** □ 允许：＿＿ □ 仅 pilot
2. **正式发布官网？** □ 允许（需域名+HTTPS） □ 仅预览
3. **生产 Fixer/Verifier？** □ 允许 □ 仅隔离
4. **C 链 D7？** □ 允许 □ 保持 BLOCKED
5. **PR #16？** □ approve+merge □ close □ 保持 OPEN
