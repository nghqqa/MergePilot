# 真实输入漂移 → ReceiptConflict 证据（记录并保留）

## 事件一（自然发生，2026-09-25 Ops 轮）

- 上下文：CONTROLLED_READONLY_PILOT_OPERATIONS，隔离 PG，run `run-canary3-tz-2`
  （nghqqa/tizhou#2，head c443150958b3b8bab0a7b85c907a74ef59206ca5）
- 过程：并发首轮审查写入 sast_scan 回执（invocation_id `req-run-canary3-tz-2-sast-scan`，
  files=10）；紧随其后的重复运行因一次瞬态 GitHub 文件抓取差异（review 的逐文件
  contents 抓取失败即跳过）导致重放输入集不同 → 输入 digest 不同
- 结果：sink 判定同 invocation_id + 不同语义内容 → **ReceiptConflict 拒绝**，
  原始回执保留、行数不变（每 run 恒 2 行）；随后三次干净重放全部 OK（幂等）
- 结论：fail-closed 冲突检测在真实条件下工作；差异重放不覆盖、不产生重复行

## 事件二（受控复现，同日 CANONICAL_CONSOLE_PROMOTION 轮）

- 目的：在 canonical console 的隔离 PG（mp-cc-pg）上以**标注为受控实验**的方式
  复现同一机制，固化证据
- 设置：run `run-canary4-tz-2` 正常写入后，以相同 invocation_id 重放 sast_scan，
  但文件列表截断为前 5 个（故意输入漂移）
- 结果：`DIVERGENT REPLAY RESULT: CONFLICT (fail-closed, original kept)`；
  行数保持 2（见下）
- 机器记录（当轮执行输出）：
  ```
  DIVERGENT REPLAY RESULT: CONFLICT (fail-closed, original kept)
  rows after divergent replay (must stay 2): 2
  ```

## 机制（receipt.py receipt_content_digest）

语义摘要覆盖全部实质字段（digests/status/binding/identity），仅排除
audit_status、started_at/completed_at、integrity、ledger_sync、cli_exit、conflict。
同 invocation_id 重放：语义一致 → no-op（或单向审计升级）；语义不同 →
ReceiptConflict。写入在事务内读行判定，跨进程由存储层唯一约束强制。

## 边界

- 两事件均在一次性隔离 PG 上发生；未触碰真实仓库（漂移源于只读 GET 抓取，
  非仓库变更）
- 事件二的截断是标注的受控实验（负向场景隔离执行原则），非自然故障
