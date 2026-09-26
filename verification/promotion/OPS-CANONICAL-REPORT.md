# CANONICAL_READONLY_OPERATIONS — 运行记录与验收

基线：console @ 1ef5032（未回退） · 能力来源 integration @ 08dbe35（只读引用）
主入口：**canonical console**（48190；demo-platform 不再作为主入口）
范围：wookat/speaktype#426 · nghqqa/tizhou#2 · 已批准 pilot 操作员（未新增任何主体）
基础设施：一次性隔离 mp-cc-net / mp-cc-pg / mp-cc-minio（无共享资源）

## 验证矩阵（ops-canonical.mjs 11/11 PASS + 1 SKIP 如实记录）

1. **登录生命周期** — 错误凭据 401 → 成功 200 → session 回显（user+allowlist）→
   携 CSRF 退出后旧会话 401；真实短 TTL（3s）会话 200→401；容器重启后旧会话 401 →
   重登恢复 POSTGRESQL_LIVE。
2. **/core 数据面** — 五 API 全 LIVE；两 PR 真实 head（dc425e12/c4431509）+PR 号
   （#426/#2）；receipts 4 行、gate 审计 3 行可见。
3. **allowlist 与隔离** — 聚合视图零越权行；?repo= 每仓纯度 100%；PG 层回执
   按仓绑定（run-canary5-tz-2 零跨仓行）。
4. **边界状态** — 401/403/404/400 + 一次性容器 BACKEND_NOT_WIRED（空数组不造假）
   与 BACKEND_ERROR（错误明细不假成功）。
5. **不变式** — stale head → gate REFUSE（SKILL_GATE_REFUSED_STALE，绝不 success）；
   重复运行行数稳定（每 run 2 行）；本轮受控输入漂移重放被 CONFLICT 拒绝、原件保留；
   TTL 过期票在库 past-due（allowlist 隐藏为设计行为）。
6. **MinIO read-back / PG audit / 浏览器** — 证据三件套写入 mp-cc-minio 读回
   sha256 逐一相符（ee0fcc5d/5d3653e7/117a4f48）；gate 审计写入 PG 并经 /api/audit
   会话内读回；浏览器真实表单登录（错误密码诚实提示）后零 JS 错误/零未处理 rejection
   （截图 artifacts/ops-session-core-page.png）。
7. **rollback 与重供** — 容器/网络清零（残留 0）→ 迁移+容器复供 → health 200。

## Skipped 项（不计入 passed）

| 测试 | 原因 |
|---|---|
| console_v3 只读 HTTP 联调（live-v3.integration.test.mjs） | MERGEPILOT_V3_URL 未设置 — console_v3 服务不在本轮拓扑；snapshot 为正式数据源，不伪装联调已发生 |

单测套件同期实跑：70 tests / 69 pass / 0 fail / 1 skip（即上表项）。

## GitHub 调用

canary5 审查 24 次调用全部 GET（canary5-run-report.json github_calls）；零写入、
零 approve/reject/push/merge、Fixer/Verifier 未启动、RAG/embedding/RUN_BINDING_AUTH 关闭。

## 判定

机器验证全部通过（11/11 + 1 skip 如实记录 + 浏览器零 JS 错误 + MinIO/PG 读回 +
rollback 复供）。停止条件零触发；范围未扩大。

**等待人工确认后**输出 CANONICAL_READONLY_OPERATIONS_VERIFIED（本报告即暂停点）。
不宣称：生产上线 · 全量用户内测就绪 · RAG 已接入 · 自动修复启用 · GitHub 写入开启。

---

## 人工确认（终判）

- 确认时间：2026-09-25（会话内，仓库 owner）
- **最终判定：CANONICAL_READONLY_OPERATIONS_VERIFIED**
