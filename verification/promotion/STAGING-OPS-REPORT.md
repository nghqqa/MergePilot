# LIMITED_READONLY_STAGING_OPERATIONS — 运维验证报告

基线：console @ a0d8dc7（未回退、未 push、零代码修改） · staging 127.0.0.1:48200
镜像 sha256:1056df76… · 独立命名卷 mp-stage-pgdata / mp-stage-miniodata · A 链保持关闭（无人工授权）

## 必验十项结果

**1 · 登录生命周期（O1）** — 错误凭据 401 → 成功 → session echo（结构化 user）
→ CSRF logout 200 → 旧会话 401 → 重登 POSTGRESQL_LIVE。

**2 · 五面 LIVE（O2）** — /overview、/pending、/repos、PR detail、audit
全部 POSTGRESQL_LIVE（五源逐一验证）。

**3 · 两 PR 与 PG 一致（O3）** — receipts 4=4（API=PG）；tizhou#2 head
c4431509 与 PG 逐字节一致；stages PASSED 2；repo/PR/run/gate 全对齐。

**4 · 边界（O4）** — 未授权 repo 403、未知 pack 404、API 零泄漏
（staging fixture 仓 stage-fixture/repo-x 在 PG 中存在但对 API 不可见）。

**5 · fail-closed（O5/O5b）** — 重复运行行数稳定（2/2）；本轮受控输入漂移
重放被 CONFLICT 拒绝（原件保留）；过期 ticket past-due 且 allowlist 隐藏；
stale head → gate REFUSE（SKILL_GATE_REFUSED_STALE）。

**6 · overview 交互** — 3 图表 canvas 渲染、PR 钻取链接存在、菜单键盘可聚焦；
source 在 <details> 详情中（展开后 POSTGRESQL_LIVE 可见——折叠时 innerText
不包含属浏览器行为，非缺陷）。

**7 · 路由切换零错误** — 五页路由切换（/pending /repos /core /settings
/overview）+ PR 详情加载，**JS error 与 unhandled rejection = 0**。

**8 · 读回** — MinIO 证据写入 staging-ops 桶并 sha256 读回一致；
PG audit 读回（API gate_decisions 2 = PG 2）。

**9 · 重启恢复（三容器同时 restart）** — health 200；旧会话按契约 401
（内存会话失效=正确行为）；**持久数据不丢失**（receipts 4→4、MinIO
staging-ops/evidence.json 仍在）；重登后 LIVE + 双仓恢复；未授权 repo
仍 403（allowlist 恢复）。

**10 · 运维记录** — staging-ops-log.json 全事件流（认证失败 1 次 401、
请求错误 0、数据源状态全 LIVE、审计连续）。

## 停止条件：零触发

无未授权泄露 · stale 未放行 · 无 receipt 未产 success · 无 BLOCKED/STALE/
ACTION_REQUIRED 显示为 PASSED（stage 前后一致）· 持久数据零丢失 ·
无 GitHub 写入（本轮零 GitHub 调用）· 无未知组件启动。

## 判定

LIMITED_READONLY_STAGING_OPERATIONS_VERIFIED
