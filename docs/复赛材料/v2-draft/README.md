# 复赛材料 v2 草案（基于 M9 轮事实 · 不覆盖 v1）

状态：草案。v1 保留于上级目录原样。本目录全部断言以
`evidence/m9-external-realcase/` 为准；争议处以证据文件为最终口径。

## 1. 外部验收状态（截至 2026-08-25）

- **EXTERNAL_BLOCKED**：无第二台独立 Windows 11 物理机（唯一外部阻塞条件）。
- 同机验收（仅验收操作）：preview.3 在全新机器语义下 **Install 阻断**
  （缺陷 B：pgvector 代码 pin 8e5355e9 / 随包 tar 7f58c993 /
  registry ccc6e83d｜a3625087 三方不一致，且 .Id 语义随存储后端漂移）。
  资产层复核全部通过（4 资产 SHA-256、9 digest 可从 tar 复现、
  preview.1/.2/.3 未改动）。
- Windows 出版边在本机被 WinNAT 保留区间内核级拒绝（缺陷 C；
  Check 未做 bind 探测）。
- 修复分支 `fix/m9-pgvector-pin-and-checksums`（含缺陷 A/B 复现测试与
  最小修复，105 项相关测试通过）已交原机器评审，未合入 main、未发布。

## 2. 真实 PR 案例页（M9 新增）

- 仓库：nghqqa/MergePilot-Demo（私）；Draft PR#1（保持未合并）。
- 受控真实缺陷：download() 路径穿越；失败测试实锤
  （`AssertionError: ValueError not raised`）。
- 初始 revision 6bef30e →（管道受阻，带外修复 a569f0d，已标注）→
  4/4 测试通过。
- 管道执行记录：diff-parse OK；risk-classify L1；sast-scan 0 findings
  （**缺陷 E：漏报真实穿越**——材料必须如实呈现）；case-retrieval
  干净失败（DB 属未起之栈）。Gateway/MCP/Receipt/审计：阻断，未执行。

## 3. 合同状态（不变）

revision_producer_contract=NOT_VERIFIED；audit_producer_contract=
NOT_VERIFIED；application/database/production 边界全部保持未验证。
理由：运行时栈未在本轮任何合规路径上完成启动。

## 4. 测试计数

- main@379744d：2246 passed / 20 skipped（冻结基线，本轮未变）。
- 修复分支：+7 项新测试（缺陷 B 复现/接受集/run-ref 契约），
  相关文件 105/105 通过；分支未合入，不计入 main 计数。

## 5. 最终 Demo 录制候选脚本（未录制）

1. 开场三句硬标注（transport=wsl-user-relay；direct_routing=false
   （经中继）；五边界 NOT_VERIFIED）。
2. §1 资产复核实拍：下载 4 资产 → sha256sum -c（修复 A 后应为 LF/
   正斜杠）→ 9 digest 与 tar index 对账。
3. Check 五关全绿 → Install →（缺陷 B 修复后的）doctor 全绿。
4. Start → Windows forwarder 出版 200/405/404/403 四连。
5. 投影三态 + 五边界 NOT_VERIFIED 特写。
6. 真实 PR 案例：Draft PR#1 时间线（初始 revision 失败测试 →
   管道 skill 输出 → 修复 → 4/4 通过）；**如实口播 sast-scan 漏报**。
7. Stop/Cleanup 零残留收尾。
（前置条件：缺陷 B/C/D 在原机器修复并发布 preview.4 后方可录制。）

## 6. 声明证据矩阵

| 声明 | 证据 |
|---|---|
| 资产完整可验 | asset-verification.json |
| 全新机器 Install 阻断 | lifecycle-smoke.json（缺陷 B） |
| Windows 出版阻断 | lifecycle-smoke.json（缺陷 C） |
| 真实缺陷+失败测试+修复 | real-pr-case.json |
| SAST 漏报（诚实呈现） | real-pr-case.json（缺陷 E） |
| 合同未验证 | gateway-decisions/receipt/audit-chain.json |
| 环境清理与披露 | cleanup.json |
