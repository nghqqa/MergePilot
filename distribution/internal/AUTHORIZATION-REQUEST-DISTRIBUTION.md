# FULL_AGENT_DISTRIBUTION — 授权请求

生成时间：2026-09-26 · 基线 9705a4f

## 分发包内容

```
distribution/
├── docker/
│   ├── docker-compose.yml       # 编排模板（healthcheck/restart/持久卷/资源限制）
│   ├── .env.example             # 环境变量模板（零真实秘密）
│   ├── mp-console-image.tar     # 镜像导出包（~60MB）
│   ├── image-digest.txt         # sha256:1056df76...
│   ├── image-sbom.cdx.json      # CycloneDX SBOM
│   └── image-trivy.txt          # Trivy 报告（0 漏洞）
└── docs/
    ├── README.md                # 总入口 + 能力矩阵
    ├── QUICKSTART.md            # 5 分钟启动
    ├── ARCHITECTURE.md          # 组件关系 + 数据流
    ├── API-CONTRACTS.md         # 五 Core API + auth 三件套
    ├── AUTHENTICATION.md        # 会话/CSRF/TTL/allowlist
    ├── REVIEW-AGENT.md          # 审查 Agent 使用说明
    ├── FIXER-VERIFIER.md        # Fixer/Verifier 使用边界
    ├── DOCKER-DEPLOY.md         # Docker 部署详解
    ├── CONFIGURATION.md         # 配置与 secrets 管理
    ├── MONITORING.md            # 运维监控
    ├── AUDIT.md                 # 审计与证据
    ├── ROLLBACK.md              # 回滚手册
    ├── SECURITY.md              # 安全模型 + 红线
    ├── CONTRIBUTING.md          # 贡献指南
    ├── LIMITATIONS.md           # 已知限制
    └── RAG-STATUS.md            # A 链关闭 + C 链 BLOCKED 说明
```

## 跨机器验证结果

| 项 | 结果 |
|---|---|
| 镜像导入（docker load） | ✓ |
| Console 启动（隔离网络+PG） | ✓ health 200 |
| 登录 + allowlist 403 | ✓ |
| A 链 a_chain_disabled | ✓ |
| PG 无 schema → BACKEND_ERROR（诚实）| ✓（部署时需先跑迁移） |
| 资源清理 | ✓ 零残留 |

## 逐项授权请求

1. **Registry push？** □ 允许：＿＿ □ 不允许
2. **Registry 地址和镜像命名？** ＿＿＿＿
3. **内部 staging 部署？** □ 允许 □ 不允许
4. **允许的用户？** □ 保持 pilot □ 增加：＿＿
5. **允许的仓库和 PR？** □ 保持两 PR □ 增加：＿＿
6. **Fixer/Verifier 持久运行？** □ 允许 □ 不允许
7. **A 链重新启用？** □ 允许 □ 保持关闭
8. **C 链继续 BLOCKED？** □ 是 □ 授权 D7 下载
9. **有效期？** □ 7 天 □ 自定义：＿＿
10. **回滚版本？** sha256:41bd3029 + 手册

**沉默、查看报告或模糊回复均不构成授权。**
