# M9 外部验收与真实 PR 案例 · 汇总

裁决：**MERGEPILOT_M9_EXTERNAL_REALCASE_BLOCKED**

## 阻断链（按序）

1. 外部独立 Windows 11 物理机不可得（唯一外部阻塞条件）。
2. 同机纯 preview.3 验收在 Install 即被缺陷 B 阻断
   （pgvector 三方字节不一致 + .Id 后端语义漂移），
   Check/资产校验/9镜像导入全部通过，doctor 单项失败。
3. Windows 出版边被缺陷 C（WinNAT 端口保留区间覆盖 8600/8090）
   内核级拒绝；Check 未做 bind 探测，最后一步才失败并全量回退。
4. 修复分支诊断性运行暴露缺陷 D（demo-console exit 1，诊断不足）。
5. 栈不可起 → §4 运行时合同（PAT 隔离/revision 绑定/stale 拒绝/
   重试幂等/审计链）无法验证 → revision/audit 合同维持 NOT_VERIFIED。

## 已完成（不依赖栈）

- §1 基线核验：fetch 前后 SHA、tag→commit、4 资产哈希、9 digest
  从 tar 复现、preview.1/.2/.3 原封未动。
- §3 真实 PR 案例：MergePilot-Demo 私仓 Draft PR#1；
  受控真实路径穿越缺陷（初始 revision 6bef30e，失败测试实锤
  "ValueError not raised"）；diff-parse OK / risk-classify L1 /
  sast-scan 0 findings（缺陷 E：漏报）/ case-retrieval 干净失败；
  带外修复 a569f0d（明确标注非管道驱动），4/4 测试通过；
  PR 保持 Draft 未合并，带验收注释。
- §5 缺陷移交：分支 fix/m9-pgvector-pin-and-checksums（3 commits，
  含 A/B 复现测试与最小修复，105 项相关测试通过）供原机器评审；
  本机不再进行开发。
- §6 本目录全部 JSON 可解析、脱敏（无用户名/主机名/绝对路径/秘密）。

## 环境遗留清理状态

见 cleanup.json。
