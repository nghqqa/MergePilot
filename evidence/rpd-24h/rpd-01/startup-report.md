# RPD-01 启动报告（2026-09-23T15:09:04Z）

- 工作目录: D:\goai\MergePilot
- 分支: feat/backend-pg-storage
- **BASE_HEAD = b79cd39cbcd78731ac50b2ff0f25b00a5a11514e**（= 最近验证提交，匹配）
- 工作树: 干净（0 条未提交项）
- 远端: origin = https://github.com/nghqqa/MergePilot.git（main @ 52f5e57；本 feature 分支尚未在远端存在）
- stash: 空
- 契约文档: 5/7 在本分支树内（ARCHITECTURE-V3/ORCHESTRATION-CONTRACT/M2-GATE-STORAGE-OPTIONS/M2-APPROVAL-SPEC/STATUS）；
  DATA-ARCHITECTURE-PG.md 与 SELFHOST-INSTALL-TARGET.md 在设计分支 docs/architecture-audit-20260922（已核实存在于该分支，未合入本分支树——记录为事实，不阻塞）
- RPD 文件: 启动时不存在 → 已按规则创建（YAML=状态源，MD=展示视图）
- 已知事实处置: pgvector 11/11、TicketStore smoke 13/13、case_retrieval 9/9、回归 281/PG 25-26 等
  全部标记为"待证据复核"——由 RPD-02/04/05 用新命令重新验证后才可置 DONE
- 秘密检查: 本报告不含任何凭证/密钥
- 预算: model_requests=0/120, tokens=0/400000（provider 无实时计量→以本地计数硬停）
