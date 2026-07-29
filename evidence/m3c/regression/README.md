# M3-C 回归证据(B4 未破坏)

M3-C 落地后,**在 fixture 仓库重跑 B4 全部门禁**,确认未引入回归。结果与已闭合里程碑一致:

| 门禁 | 脚本 | 结果 | 闭合里程碑 |
|---|---|---|---|
| B4c.1.6 hardening | `m3b-b4c1_6-hardening.sh` | **5/5** | m3b-b4c1.6-closed |
| B4e 总 E2E | `m3b-b4e-e2e.sh` | **43/43** | m3b-b4e-closed |
| B5 负向 | `m3b-b5-negative.sh` | **50/50** | m3b-b5-closed |

## 文件
- `b4c/b4c-e2e-test.out` — B4c-5 E2E(PASS=42 FAIL=0)
- `b4e/b4e-e2e-test.out` — B4e 总 E2E(PASS=43 FAIL=0)
- `b4e/db-snapshot.txt` — B4e 终态快照
- `b5/b5-negative-test.out` — B5 负向(PASS=50/50 FAIL=0)
- `b5/mcp-calls-window.txt` — B5 审计窗口

## 说明
- 这些是**已闭合里程碑的 E2E 输出**;M3-C 落地后重跑,pass 计数与里程碑一致(B4c.1.6 5/5、B4e 43/43、B5 50/50),
  证明 M3-C 的 controller/gateway_client/probe-tools 改动**未破坏 B4 L2 链与负向门禁**。
- 关键关联:M3-C 复用 B4 的 `l2_ensure_ticket`/`drain_l2_outbox`(child run 走正常 drain→MERGED);
  `gateway_client` 仅**新增** `gateway_get_commit`/`gateway_list_commits`/`gateway_get_file_text`(未改既有 L2 调用);
  `probe-tools` 改为优先输出 EmbeddedResource 真内容(对 JSON-only 工具回退 text,不影响 B4 解析)。
- 原 B4c/B4e/B5 里程碑证据(`evidence/m3b-b4*`)已恢复到闭合状态,未被改写;此处为独立回归副本。
- 过程发现并已修复:M3-C 首跑曾因假 room 的 `dispatch_outbox` 残留干扰 B4e Loop B(首跑 42/43);
  `m3c-e2e.sh` cleanup 改 FK 安全顺序 + 新增 **M3-C DB residue=0** 硬门后,B4e 恢复 43/43。
