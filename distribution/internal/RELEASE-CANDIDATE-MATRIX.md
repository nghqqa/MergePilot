# FULL_AGENT_INTERNAL_RELEASE_CANDIDATE — 能力矩阵与内测说明

日期：2026-09-26 · 提交：43ab9b4 · 镜像：ghcr.io/nghqqa/mergepilot-console:rc-20260926

## 十二项核验结果

| # | 核验项 | 结果 | 证据 |
|---|---|---|---|
| 1 | Review Agent 只读链路 | ✓ | POSTGRESQL_LIVE、两 PR PASSED |
| 2 | 隔离 clone 闭环 | ✓ 27/27 | fxv-closure-report.json |
| 3 | 失败闭锁/stale/drift/并发/重启 | ✓ | 负向矩阵 10/10 |
| 4 | GHCR digest 一致 | ✓ | 1056df76 local=remote |
| 5 | SBOM/Trivy/导入/回滚 | ✓ | 0 漏洞、export/import 一致、41bd3029 可用 |
| 6 | Console live API/认证/allowlist/审计 | ✓ | 14/14 回归 |
| 7 | A 链 reference-only | ✓ | 当前 ON（promote staging） |
| 8 | C 链 BLOCKED | ✓ | 三缺口不变 |
| 9 | embedding/pgvector/RUN_BINDING_AUTH | ✓ | 全部关闭 |
| 10 | 真实 PR 只读零变化 | ✓ | 4\|0\|2 不变、GitHub 写入=0 |
| 11 | 文档/部署/监控/回滚/限制 | ✓ | 16 篇 + compose + env 模板 |
| 12 | 本矩阵 | ✓ | 本文档 |

## 最终能力矩阵

### ✅ 已就绪（可供内测使用）

| 能力 | 验证 | 入口 |
|---|---|---|
| Console 只读查询 | E2E 8/8 + user-pilot 4/4 + UA 14/14 | http://127.0.0.1:48400 |
| PR 阶段推导 | 合同测试 + overview LIVE | /api/overview |
| Session/CSRF/TTL/allowlist | 401/403/404 矩阵全过 | /api/auth/* |
| A 链组织知识检索 | preflight 11/11 + CI 12/12 + LUO 10/10 + pilot 10/10 | /api/rag/org-search |
| Fixer（隔离 clone） | preflight 11/11 + staging 11/11 + closure 27/27 | 隔离工作区 |
| Verifier（独立判定） | preflight 7/7 + closure 含 | 隔离工作区 |
| Fixer→Verifier 联调 | iso_chain 29/29 + canary 20/20 + closure 27/27 | 隔离 clone |
| Docker 分发 | export/import digest 一致 | distribution/docker/ |
| GHCR 镜像 | push + digest 一致 + Trivy 0 | ghcr.io/nghqqa/mergepilot-console:rc-20260926 |
| 回滚 | 41bd3029 可用 + 演练多轮 | distribution/docs/ROLLBACK.md |

### ⛔ 未就绪（需操作员/Owner 行动）

| 能力 | 阻塞原因 | 阻塞者 |
|---|---|---|
| C 链（skill_case_retrieval） | model cache + metadata attest + key distribution | 操作员 |
| Fixer 处理真实 PR | 需真实 PR 含 finding + GitHub 写入授权 | Owner + GitHub |
| Verifier 发布真实 PR 修复 | 需 push 权限 | Owner + GitHub |
| 自动 merge/approve | 设计上禁止（人工门） | 架构决定 |

## 内测说明

### 适用用户
仅 `pilot`（当前唯一已批准操作员）

### 适用范围
- wookat/speaktype#426（只读）
- nghqqa/tizhou#2（只读）

### 允许的操作
1. 登录/退出 Console
2. 浏览 overview/pending/repos/PR detail/audit（只读）
3. A 链 reference-only 查询（组织规范参考）
4. 观察错误/过期/stale/degraded/恢复行为
5. 隔离 clone 中的 Fixer→Verifier 链路验证

### 禁止的操作
1. 任何 GitHub 写入（push/merge/approve/reject/comment/label）
2. 修改真实 PR
3. 启用 C 链/embedding/pgvector
4. 启动生产 Fixer/Verifier
5. 扩大用户/仓库/PR 范围
6. 将 BLOCKED/STALE/ACTION_REQUIRED/DEGRADED 显示为 PASSED

### 停止条件（立即终止内测）
- 越权数据泄露
- 真实 GitHub 写入
- stale 或无 receipt 产生 success
- reference-only 影响风险决策
- 持久数据丢失

### 回滚
- 镜像：sha256:41bd3029（mp-canonical-console:rollback-prev）
- 命令：见 distribution/docs/ROLLBACK.md
- A 链：去 env 重建容器 → a_chain_disabled

---

**判定：FULL_AGENT_INTERNAL_RELEASE_CANDIDATE_READY**
