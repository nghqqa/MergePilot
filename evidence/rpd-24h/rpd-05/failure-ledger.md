# RPD-05 回归失败清单（2026-09-23 ~16:20 UTC）

## 本地四目录回归（approval + gh_bridge + model_gateway + skills）
- **282 passed / 29 skipped / 0 failed**（regression-local.txt）

## PG 门控套件（MERGEPILOT_PG_CONTRACT=1，隔离实例 mp-pg-contract-test）
- **25 passed / 1 failed**（regression-pg-gated.txt）
- 唯一失败 = tests/approval/test_store_pg.py::PgSpecificAcceptance::test_cross_process_race_single_winner
  - 根因：Windows spawn 子进程结果队列 Empty（基线既有，与 25c3a70/59ccd1a 改动无关——
    上轮已做 stash 对照验证：还原 pg_store.py 后同样失败）
  - 处置：保持 TEST-DEBT 登记，不改测试标准，不 skip 掩盖，不 xfail 隐藏
- 结论：无新增相关回归；既有失败清单与 TEST-DEBT.md 口径一致

## 隔离 Docker/PG smoke（本轮 RPD-02 重放）
- pgvector_smoke.py 11/11（一次性实例，重放后销毁）
