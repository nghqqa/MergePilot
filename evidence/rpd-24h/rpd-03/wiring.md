# RPD-03 case_retrieval 接线契约（2026-09-23 ~15:45 UTC）

## 本轮新增
- validate_env.py 新增 `--preflight`：真实连接 DSN（只读会话）+ 只读角色校验 + 表能力校验（零业务查询）。
- 实测（一次性隔离实例，验证后销毁）：
  - 正向：env(只读 DSN+桥生成 scope file) → `ok preflight: connection + read-only role + table capability verified` → **exit 0**；
  - 负向：指向不存在数据库 → `FAIL preflight: CASE_RETR_DB_UNAVAILABLE (sanitized, no DSN details)` → **exit 5**。
- 修复：_preflight_db 的仓库根路径深度错误（dirname×3→×4），此前 adapter import 必失败。
- 离线回归：preflight 开关与脱敏断言入 tests/gh_bridge/test_case2_fixes.py。

## 既验契约（此前轮次+本轮复核）
- scope 唯一可信来源 = gh_bridge authored_by 的 run-context（顶层 repo 形状已兼容，第三十五轮修复）；
- 缺 DSN→CASE_RETR_DB_UNAVAILABLE(2)；缺 scope/作者不符/文件缺失→SCOPE_MISSING(3)；env-file 不一致→exit 3；
- 适配器 SQL 结构性以 WHERE repo_scope=%s 起始——不存在无 scope 全库回退；
- DSN 值从不打印。

## 状态
隔离接线契约=完成；正式共享 controller/Worker 注入=**不属于本轮自动权限（WAITING_HUMAN，已单独列出授权清单）**。
