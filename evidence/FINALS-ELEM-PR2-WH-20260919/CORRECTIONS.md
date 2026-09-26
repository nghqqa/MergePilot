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

4. **run 标识 085056 / 085058 关联说明(勘误追加,不改原件)**:本包两条 run 标识指向同一交付
   (PR #2 synchronize @ 65de83d6,delivery_id 38c1c5f0)。`run-gh-pr2-65de83d6-085056` 为**桥侧
   收单时刻打点的标识**(delivery-ledger received_at 08:50:55.97Z),出现在门审批记录
   human-gate-approval.md、门批准 DM、check-run.json 摘要与 skill-audit.json 的 run 字段;
   `run-gh-pr2-65de83d6-085058` 为**运行时项目实例化时刻的执行标识**(claimed_at 08:50:58.01Z),
   kickoff、plan/meta 与全部任务工件(review-1/fix-1/verify-1)及团队房消息均携带该标识。两者
   时间差 2 秒,系桥侧与运行时分别取号所致,不存在第二个执行实例工件。关联判定依据:门批准 DM
   所复核的审查任务、check-run 内嵌 leader report(Run ID …085058)与全部任务工件指向同一实例。
   另:08:51:08Z 出现跨轮任务库冲突(06:45 的 1414edb 轮已完成 gh-pr2-review-1,submit 被拒),
   Leader 重规划后以新任务 ID 继续——即附录 D「碰撞与重规划」事实本体,与上述双标识现象相互独立。

PR3/PR1 包同步执行第 1、2 项修正(该两包无第 3 项问题)。
