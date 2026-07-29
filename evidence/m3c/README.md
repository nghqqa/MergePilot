# M3-C 验收证据索引

M3-C(状态感知失败处理 + 回滚,child-run 模型)落地证据。HEAD=`7c12094`(未提交)。

## fresh-DB 闭合验证(本目录 `fresh-db-migration.log`,**原始结果未删改**)

全新空库依次铺设完整迁移链 `init → m3_state → m3b_policy → m3b_b4 → m3b_b4c → m3b_b4c1 → m3b_b4c1_1 → m3b_b4d1 → m3c_state`:

| 项 | 结果 |
|---|---|
| fresh DB **首次完整迁移** | **9/9 rc=0 — PASS** |
| M3-C Schema 断言(FK/UNIQUE/CHECK/函数/列) | **19/19 — PASS** |
| `m3c_state.sql` **独立重入**(单文件再跑) | **rc=0 — PASS(M3-C 自身幂等)** |
| 历史全链**二次重放** | **8/9** —— `m3b_b4.sql` 为**已知 XFAIL**,不属于 M3-C 代码失败(见下) |

> **措辞边界**:本目录只断言"**fresh DB 单次铺设成功,M3-C 自身幂等**";**不**声称"全迁移链完全幂等"。
> 历史全链二次重放的 8/9 失败发生在 **B4 既有迁移**(`m3b_b4.sql`),与 M3-C 代码无关。

## 技术债 MIG-B4-001(B4 迁移链,非 M3-C)

- **现象**:全链二次重放时 `m3b_b4.sql:193 ERROR: cannot remove parameter defaults from existing function`。
- **根因**:`m3b_b4d1.sql` 把 `l2_approve` 演化为带 `DEFAULT NULL` 的签名;早版 `m3b_b4.sql` 的 `CREATE OR REPLACE`
  无法回退已存在函数的参数默认值(PostgreSQL 限制)。即 B4 链经 B4d.1 签名演化后,**早版迁移不能再跨版本重放**。
- **支持的生产路径**:按版本 **forward-only 单次应用**(每个迁移文件只 apply 一次,按依赖序)。生产部署不受影响。
- **处置要求**:在 **M7 空环境最终复现前**解决 —— 方案为引入正式 **migration runner 记录版本/已应用集**(如 schema_migrations 表 +
  逐文件单次应用,禁止跨版本重放),或把 `m3b_b4.sql` 的 `l2_approve` 定义改为与 B4d.1 一致的带默认签名。
- **归属**:B4 边界,不在 M3-C delivery 范围;M3-C 的 `m3c_state.sql` 本身完全可重入。

## M3-C 功能验收(`m3c-test.out` / `m3c-transcript.txt`)

- **M3-C E2E**:33/33 连续两次稳定(EXPECTED=33,FAIL=0)。
- 子项覆盖:重复事件幂等、未合并 FAIL 重试/超 MAX HOLD、POST_MERGE_VERIFY_FAILED 真实入口(process_event,verifier,
  校验 room/run/repo/pr/result_sha)、伪造拒、child run、revert→approve→merge→ROLLED_BACK→reverify PASS→RECOVERED、
  reverify FAIL→HOLD、不二回滚、drain 跳过 PENDING、显式 Gateway→CLAIM_MISMATCH、M3-C DB residue=0、容器日志落盘、无凭据/无 AI 标识。

## 回归(`regression/`,证明 M3-C 未破坏 B4)

M3-C 落地后重跑 B4 全门禁,与闭合里程碑一致:`regression/b4c/`(B4c 42)、`regression/b4e/`(B4e 43/43)、`regression/b5/`(B5 50/50)、
B4c.1.6 hardening 5/5。详见 `regression/README.md`。原 B4c/B4e/B5 里程碑证据(`evidence/m3b-b4*`)保持闭合状态,未被改写。

## 文件清单

- `fresh-db-migration.log` — fresh-DB 全链 + 断言 + 幂等重放(含 B4 XFAIL 原始错误,未删改)
- `m3c-test.out` / `m3c-transcript.txt` — M3-C E2E 33/33
- `db-snapshot.txt` / `rollback-runs.txt` / `run-raw.log` / `controller-logs.txt` — M3-C 运行快照与日志
- `regression/` — B4 回归副本
