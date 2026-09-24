# RPD-06 有限前端契约对齐（2026-09-23 ~16:40 UTC）

## 交付
- tools/console_pg/gate_display.py：八态展示契约（pending/action_required/
  approved_plan_ready/rejected/blocked/expired/backend_unavailable/scope_missing），
  纯函数；环境态（backend_unavailable/scope_missing）由显式标志覆盖票据映射；
  未映射状态 ValueError（前端不猜）。
- tools/console_pg/server.py：/api/approvals 列表与单条响应增加 `gate_display`
  字段（无新增端点/无新 API 契约/无 OAuth/无合并能力）。

## 明确不做（触发即 DEFERRED 的项未触发）
- OAuth、正式审批入口、合并能力、任何写式 API —— 均未实现。

## 验证
- gate-display-tests.txt：5/5 passed
- 受影响回归（console_pg+approval+gh_bridge+model_gateway+skills）：287 passed / 41 skipped
