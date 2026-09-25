# CANONICAL_CONSOLE_RELEASE_CANDIDATE_FINALIZATION — 终验报告

基线：fc7e0ec（未回退，未 push） · 镜像 sha256:1056df76…（Trivy TOTAL=0 CRITICAL=0）

## 1 · 提交关系与文件边界
线性单亲链：6458a12（A 链预检）→ 86cce91（用户反馈文档，并行会话）→
62c32b7（A 链受控接入，本会话）→ fc7e0ec（OVERVIEW_REMEDIATION：P0 allowlist
强制 + 统一 live 源 + P1 批次，并行会话）。文件边界清晰互不重叠：
86cce91 仅 2 文档；62c32b7 仅 8 个 A 链文件；fc7e0ec 为整改 19 文件。

## 2 · 工作树
零未提交、零未跟踪、零 ahead；`git clean -nd` 无候选——无意外生成文件。

## 3-4 · 重建与全套
- npm run build ✓（bundle 正常）
- 后端测试 **79 tests / 78 pass / 0 fail / 1 skip**（fc7e0ec 新增 remediation 测试）
- promotion E2E **8/8**（P2 断言对齐 fc7e0ec 的结构化 user 契约——行为正确，
  旧断言过时）
- user-pilot **4/4**

## 5 · 复验（fc7e0ec 修复项全部生效）
- allowlist：未知 repo pack → 404（P0 修复生效）；/api/overview LIVE 且零越权
- live 数据源：health primary=contract_v2（DSN 配置时）
- PR 钻取：/api/pulls/2?repo=nghqqa/tizhou 返回 tizhou 数据
- trend：14 天骨架 + 当日 run 计入（isoDayOf 修复）
- 图表：浏览器实测 3 canvas 渲染于 /overview
- logout：同登录 cookie+CSRF → 200，随后会话 401；session echo 结构化 user {name}
- 键盘：菜单首项可聚焦（"运营总览"）；18 菜单项可达

## 6 · A 链复验
- preflight **11/11**、integration **12/12**（known-hit/合法空/degraded/审计 MinIO
  sha256 读回/风险隔离/真实 PR 回归/控制台边界全过）
- flag-off 默认：a_chain_disabled 诚实态（一次性容器实测）

## 7 · SBOM/Trivy/rollback/清理
- CycloneDX SBOM + Trivy 表归档；镜像 0 漏洞
- rollback 演练：容器/网络/本地 rag-live 进程清零（残留 0）→ 复供 health 200
  （诚实空库态）

## 8 · 未做（按约束）
未 push、未部署到任何持久环境、未启用 embedding/C 链/Fixer/Verifier/GitHub 写入。

## 判定

**CANONICAL_CONSOLE_RELEASE_CANDIDATE_READY**
