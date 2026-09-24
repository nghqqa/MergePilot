# RPD-02 pgvector 隔离证据复核（2026-09-23 ~15:25 UTC）

## 静态证据（b79cd39）
- tools/case_retrieval/deploy/pgvector_smoke.py 存在、语法可解析（ast.parse ok）、提交于 b79cd39
- smoke 含 12 个 check( 断言点（11 项判定输出）
- 版本记录在案：PostgreSQL 16.15 + pgvector 0.8.6（README §pgvector 隔离验证 + PROGRESS 第三十四/三十五轮）

## 动态重放（一次性隔离实例，非共享 case-pg）
- 重建 pgvector/pgvector:pg16 容器（随机口令）→ 建库 cr_pg_smoke + vector 扩展 → 重放 smoke
- 结果：**11/11 passed**（含有效 scope 相似度查询、只读角色写入拒绝、五路 fail-closed、脱敏断言）
- 重放后容器立即销毁（docker rm -f）；无残留资源
- 共享 case-pg（elemiso-case-pg）：本轮零连接、零命令（审计=本会话命令记录）
- 结论：b79cd39 的 pgvector smoke 结果**可复现**，"11/11"维持
