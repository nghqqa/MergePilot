# CONTROLLED_REFERENCE_RAG_PILOT — 人工授权请求

生成时间：2026-09-25T23:35+08:00 · 账本：verification/gate/RPD-LEDGER.json

## 当前状态（全部机器验证通过）

- console：RC READY → staging 部署 8/8 → 运维 10/10 → 用户验收 VERIFIED
- A 链（org_rag_a_chain）：预检 11/11 → 受控接入 12/12 → staging 启用 9/9 → 用户运维 10/10
- C 链（case_retrieval）：BLOCKED（三缺口，单独记录不与 A 链混合）
- 镜像 sha256:1056df76（Trivy 0）、工作树清洁、9b4fd82 后零漂移
- staging 127.0.0.1:48200：health 200、LIVE、A 链 ON（reference-only）

## 请逐项明确确认以下授权

### 1. 操作员名单

当前：仅 `pilot`。
□ 保持仅 pilot
□ 增加操作员：＿＿＿＿（GitHub login + node_id，须逐个列出）

### 2. 仓库和 PR allowlist

当前：`wookat/speaktype#426` + `nghqqa/tizhou#2`。
□ 保持不变
□ 增加仓库/PR：＿＿＿＿（owner/repo#number，须逐个列出）

### 3. A 链是否持续启用

当前：ON（reference-only，feature flag 控制）。
□ 持续启用（reference-only，不参与风险决策）
□ 关闭

### 4. 是否允许持久 staging 变更

当前：mp-stage-console 以 unless-stopped 持久运行。
□ 允许保持持久运行
□ 改为按需启动

### 5. 有效期

□ 7 天（至 2026-10-02，与 pilot 授权 TTL 对齐）
□ 自定义：＿＿＿＿

### 6. 观察指标（授权期间自动记录）

- 检索审计五字段连续性（snapshot_id/query_hash/source_refs/service_state/run_id）
- degraded 发生频率与恢复时长
- 两 PR receipt/gate/ticket/stage 零变化（每轮核验）
- 零风险字段污染（finding/severity/verdict/approved 逐响应排除）
- 零越权泄露（allowlist 403/404 边界持续有效）

### 7. 停止条件（沿用）

- reference-only 改变 gate/stage/ticket/success
- degraded 显示为正常成功
- 越权泄露 / 持久数据丢失 / GitHub 写入
- embedding/C 链/Fixer/Verifier 意外启动

### 8. 回滚

- 镜像：mp-canonical-console:rollback-prev（sha256:41bd3029）
- 命令：见 A-CHAIN-OPS-MANUAL.md 回滚节
- A 链单独立即可关（去 env 重建容器）

---

**沉默、模糊回复或仅查看本报告均不构成授权。须逐项明确回复。**
