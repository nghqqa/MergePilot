# CORRECTIONS — 2026-09-19 证据勘误(导出管线修正,运行原始产物未动)

1. **skill-audit.json 窗口重切**:原导出为会话累计口径(10 条,含 PR3 窗口 4 条混入);
   已按本案窗口(08:50:55Z→09:17:50Z)重切为 6 条(审查段 3 含一次 ERROR→重试实录,验证段 3)。
   源为本包原锁定导出——其中 08:51:25 的 ERROR 条目现已被 rag-live 服务 recent 上限挤出在线流,
   仅存于本包,恰好构成"锁定导出"的必要性证明。
2. **delivery-ledger.json 重导出**:原导出为空清单(打包时 SSH 瞬断,查询静默失败);
   已从服务器台账重导出(PR#2 synchronize @ 65de83d6d061413ec98c1e79515f470313ef9806, PROCESSED)。
3. **人工门审批记录 head SHA 字段误填(勘误,不改原件)**:project/human-gate-approval.md 第 5 行
   `Head SHA under review: 65de83d000000000000000000000000000000000` 为投递时的占位符误填
   (65de83d+补零),实际 head 为 `65de83d6d061413ec98c1e79515f470313ef9806`。同包 fix spec/
   findings/plan 及验证报告中的 head 均正确;Verifier 独立复现补丁 sha256 `674356fc…16081`,
   与 head 无关的门决策语义(批准范围/委派约束)不受影响。原件保留不改,以本勘误为准。

PR3/PR1 包同步执行第 1、2 项修正(该两包无第 3 项问题)。
