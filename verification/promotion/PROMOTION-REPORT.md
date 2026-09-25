# CANONICAL_CONSOLE_PROMOTION — 验收报告

基线：integration feat/r6-integration @ 08dbe35（未回退；作为已验证能力来源，只读引用）
本仓：console worktree（feat/admin-console 之上新增本轮提交）
范围：wookat/speaktype#426 · nghqqa/tizhou#2 · 已批准 pilot 操作员（未扩大）
镜像：mp-canonical-console:candidate sha256:3f1a228d…（Trivy TOTAL=0 CRITICAL=0 HIGH=0）

## 1. 五 Core API 合并（A）
/api/pulls · /api/pending · /api/tickets · /api/evidence · /api/audit 全部迁入
console/backend（lib/core-pilot.mjs + server.mjs 路由）。/api/audit gate 决策按
allowlist 过滤 decision.repo 已知行。契约测试 5 项新增（console/backend/test/
core-pilot-api.test.mjs）；全套 70 测试 69 通过 0 失败（1 跳过）。

## 2. 会话/退出/TTL/allowlist（B）
lib/session.mjs：契约 v2 对齐 —— mp_session（HttpOnly、SameSite=Strict、HMAC
opaque sid）、GET /api/auth/session 401 JSON {error:{reason}}、X-CSRF-Token 强制
（logout 缺令牌 403）、CONSOLE_SESSION_TTL_MS 可配（短 TTL 实测 200→401）、
timing-safe 凭据比对、CONSOLE_REPO_ALLOWLIST 行级过滤 + ?repo= 越界 403。

## 3. /core 页面（C）
frontend/src/pages/CorePage.jsx + 侧栏导航"核心控制面"（默认落地 /pending
不变，IA 保留）。LoginPage 增加真实具名操作员表单（POST /api/auth/login，
成功与否由服务端会话决定；演示预览入口保留；OAuth 仍为 D-9 路线）。

## 4. 页面真实显示（D）
浏览器实证（截图 artifacts/core-page-canonical-console.png）：表单登录 →
POSTGRESQL_LIVE、两仓真实 repo/PR#（#426/#2）/head SHA、receipts、gate 审计、
待处理队列（诚实零值）。

## 5. 诚实状态（E）
401（契约形状）· 403（repo_not_in_allowlist）· 404 · 400（坏 JSON/超限）·
BACKEND_NOT_WIRED（空数组不造假）· BACKEND_ERROR（错误明细不假成功）。
500：六类真实故障探针全部映射到专属状态码（200-empty/400/404）——外部可触发
的诚实 500 不存在；末路映射（e.status ?? 500）代码验证、保持不人为强造。

## 6. 同镜像隔离 E2E（F）— promotion-e2e.mjs 8/8
一次性隔离 mp-cc-net/mp-cc-pg/mp-cc-minio；会话生命周期（含 CSRF+真实短 TTL
过期）、五 API LIVE+零 allowlist 泄漏+真实 head、403/404/400、NOT_WIRED/ERROR
一次性容器、重启语义（旧会话 401→重登 LIVE）、幂等不变式。

## 7. 两真实 PR 只读审查（G）
canary4：并发 GET-only 审查（24 次调用全 GET），回执 OK·BOUND·RECORDED
写入 console 的隔离 PG；gate 审计（含 repo/pr 上下文）落库并可经 /api/audit 读回。

## 8. SBOM/Trivy/digest + rollback（H）
CycloneDX SBOM + Trivy 表 + digest 归档（artifacts/）。Rollback 演练：
容器/网络清零（残留 0）→ 复供（迁移+容器）→ /api/health 200，/api/pulls 返回
POSTGRESQL_LIVE + 空数组（诚实空态，不造假数据）。

## 9. 输入漂移 CONFLICT 证据（I）
input-drift-conflict-evidence.md：自然事件（Ops 轮，瞬态抓取差异 → CONFLICT
拒绝、原件保留）+ 受控复现（本轮标注实验，截断文件列表 → CONFLICT，行数恒 2）
+ 机制说明（receipt_content_digest 排除集）。

## 判定

**CANONICAL_CONSOLE_READONLY_VERIFIED**

停止条件零触发：无真实 GitHub 写入/approve/reject/push/merge、未启动
Fixer/Verifier、RAG/embedding/RUN_BINDING_AUTH 关闭、未访问共享 PG/MinIO、
未扩大用户/仓库/PR 范围。

不宣称：生产上线 · 全量用户内测就绪 · RAG 已接入 · 自动修复启用 · GitHub 写入开启。
