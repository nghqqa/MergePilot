# CANONICAL_CONSOLE_LIMITED_READONLY_STAGING_PROMOTION — 部署与验证报告

基线：ea6b8ef（未回退，**未 push**——按人工确认明确不推送代码） ·
镜像 sha256:1056df76…（与 SBOM/Trivy 记录一致）

## 人工确认（第一阶段）

- 目标环境：**本机持久隔离 staging**（宿主 Docker）
- 地址：127.0.0.1:48200（回环，仅本机访问）
- 操作员：pilot（唯一；未新增）
- 仓库范围：wookat/speaktype#426 + nghqqa/tizhou#2（未扩大）
- push 与部署授权：**部署=是（本机）；push=否**

## Staging 拓扑（持久 + 一次性隔离）

- network `mp-stage-net`；PG `mp-stage-pg`（随机密码、命名卷 mp-stage-pgdata、
  restart=unless-stopped）；MinIO `mp-stage-minio`（随机凭证、命名卷）；
  console `mp-stage-console`（restart=unless-stopped、随机 session secret、
  A 链 flag **未设置=默认关**）
- 数据：outbox/ticket-store 迁移 + 两授权 PR 的 GET-only 审查（run-stage-*，
  24 次 GitHub 调用全 GET）+ gate 审计（repo/pr/head 上下文）

## 部署前验证（staging-verify.mjs 8/8）

| # | 验证 | 结果 |
|---|---|---|
| S1 | 镜像 digest 与记录一致 | ✓ 1056df76 |
| S2 | health=200 | ✓ |
| S3 | session echo + CSRF logout 200→401 + 重登 LIVE | ✓ |
| S4 | 未授权 repo 403 / 未知 pack 404 / overview 零越权 | ✓ |
| S5 | /overview /pending /repos PR-detail audit 统一 LIVE | ✓ |
| S6 | 两 PR receipt/gate/stage 与 PG 一致 | ✓ 4=4，PASSED 2 |
| S7 | A 链 flag-off：a_chain_disabled 诚实态 | ✓ |
| S8 | embedding/C 链/Fixer/Verifier/RUN_BINDING_AUTH 无暴露 | ✓ |

浏览器可用性：/overview 登录后 3 canvas 图表、trend、PR 钻取链接、
菜单键盘聚焦全部正常（1440×900）。

## Rollback 验证

1. 切换上一候选镜像（41bd3029，r4-remediation tag）→ health 200、
   pulls LIVE 2 条 ✓
2. 清理 staging console 容器（PG/MinIO 命名卷保留——持久数据不动）✓
3. 重新部署当前候选 → health 200 + staging-verify **8/8 回归通过** ✓

## 边界维持

GitHub 只读（零写入）· Fixer/Verifier 未启动 · A 链 flag-off（显式控制）·
skill_case_retrieval/embedding/pgvector/model cache 关闭 ·
RUN_BINDING_AUTH=NOT_WIRED · 独立 PG/MinIO/network/secrets（无共享资源）·
仅两授权 PR · 停止条件零触发。

## 内测期间允许的验证范围（as-is 记录）

登录/退出；overview、pending、仓库、PR、审计只读浏览；A 链 reference-only
查询（当前 flag-off，如需启用须另行确认）；错误/过期/stale/degraded/恢复行为；
页面可用性与数据一致性。

## 判定

**LIMITED_READONLY_STAGING_READY**

不宣称：生产上线 · 全量内测就绪 · 完整 RAG 已接入 · 自动修复启用。
