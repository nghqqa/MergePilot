# CONTROLLED_READONLY_USER_PILOT — 验收报告

基线：console @ 807e0bc（未回退） · 入口：canonical console（48190）
用户集：**pilot**（唯一已批准内部试用操作员；本轮未新增任何主体——约束 7）
范围：wookat/speaktype#426 · nghqqa/tizhou#2（未扩大）
基础设施：一次性隔离 mp-cc-net / mp-cc-pg / mp-cc-minio（无共享资源）

## 验证矩阵

**1 · 每用户只见授权仓库（user-pilot.mjs U1）**
pilot 会话可见集 = 恰好两授权仓；PG 中确有未授权仓行（pilot-staging/repo-a、
other-org/outsider-repo fixtures）——对用户全部不可见（负材料在库、视图零泄漏）。
浏览器侧同样验证：页面无任何 fixture 仓字符串。

**2 · 认证边界与恢复（U2）**
未认证 401 · 未授权仓 403 · 错误密码 401 · 携 CSRF 退出后旧会话 401 ·
真实短 TTL（3s）会话 200→401 · 容器重启后旧会话 401 → 重登恢复 POSTGRESQL_LIVE。

**3 · /core 数据一致性（U3）**
API ↔ PG 交叉核对：两 PR head 与库内逐字节一致（c4431509/dc425e12），
PR 号 #2/#426 正确，canary6 receipts 计数 API=PG（4=4），gate 审计计数 API=PG（2=2）。

**4 · fail-closed 不变式（U4）**
stale head → gate REFUSE（SKILL_GATE_REFUSED_STALE，绝不 success）·
重复运行行数稳定（每 run 2 行）· 本轮受控输入漂移重放被 CONFLICT 拒绝、
原件保留（helpers/drift-replay.py 输出在案）· TTL 过期票库内 past-due 且
对用户不可见（allowlist 设计行为）。

**5 · 浏览器（U5）**
真实表单登录 → LIVE 全要素视图；**零 JS 错误、零未处理 rejection**
（导航前安装采集器）；页面无未授权仓泄漏。

**6 · MinIO / PG audit / rollback（U6）**
user-pilot 证据三件套写入 mp-cc-minio 读回 sha256 逐一相符
（8422259c/5d3653e7/d913c567）；gate 审计写入 PG 并经 /api/audit 会话读回；
rollback 演练：容器/网络清零（残留 0）→ 复供迁移+容器 → health 200。

**7 · 范围纪律**
未新增仓库/PR/写权限/操作员。新增任何主体须暂停人工确认——本轮无新增，
未触发。GitHub 调用 24 次全 GET（canary6-run-report.json）；零写入、
零 approve/reject/push/merge；Fixer/Verifier 未启动；RAG/embedding/
RUN_BINDING_AUTH 保持关闭。

## 判定

**CONTROLLED_READONLY_USER_PILOT_VERIFIED**

不宣称：生产上线 · 全量用户内测就绪 · RAG 已接入 · 自动修复启用 · GitHub 写入开启。
