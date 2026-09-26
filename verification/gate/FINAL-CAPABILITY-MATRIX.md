# PROJECT_AUTONOMOUS_CLOSURE_FINAL_GATE — 能力矩阵与最终授权请求

基线：60bf797（未回退） · 2026-09-26

## 一 · 核验结果（10/10 通过）

| # | 核验项 | 结果 | 证据 |
|---|---|---|---|
| 1 | git 工作树/提交链/ledger 零漂移 | ✓ | 0 未提交、0 未跟踪、FXV_VERIFIED |
| 2 | Review Agent 只读 + 两 PR 一致 | ✓ | POSTGRESQL_LIVE、PASSED 2、4\|1\|2 |
| 3 | Fixer preflight | ✓ 11/11 | FP-1~10 + FP-7b/9b 修正 |
| 4 | Verifier preflight | ✓ 7/7 | VP-1~7 |
| 5 | Fixer→Verifier 隔离联调 | ✓ 18/18 | fxv-isolated.mjs A1-A6/B1-B5/C1-C6/D1 |
| 6 | 真实 PR 零变化 | ✓ | PG 4\|1\|2 不变、PASSED 2 不变 |
| 7 | A 链关闭 | ✓ | a_chain_disabled |
| 8 | 镜像 digest/SBOM/Trivy/rollback | ✓ | 1056df76、0 漏洞、export/import 一致、41bd3029 可用 |
| 9 | 隔离资源清理 + 无秘密入工件 | ✓* | mp-rc-* 已清、仅 mp-stage-* 保留；*见 P3 发现 |
| 10 | 能力矩阵 + 未完成项清单 | ✓ | 本文档 |

## 二 · 能力矩阵

### 已就绪（代码模块 + 测试通过）

| 能力 | 状态 | 证据 |
|---|---|---|
| Console 只读查询（五面 LIVE） | ✅ READY | E2E 8/8 + user-pilot 4/4 |
| PR 阶段推导（权威枚举） | ✅ READY | /api/overview 合同测试 |
| Session/CSRF/TTL/allowlist | ✅ READY | 401/403/404 矩阵 |
| A 链组织知识检索（lexical） | ✅ READY（已关闭） | preflight 11/11 + CI 12/12 + staging 9/9 + LUO 10/10 |
| Fixer 隔离模式 | ✅ READY（fixture 级） | preflight 11/11 + FXV A1-A6 |
| Verifier 隔离模式 | ✅ READY（fixture 级） | preflight 7/7 + FXV B1-B5 |
| Fixer→Verifier 联调 | ✅ READY（fixture 级） | iso_chain 29/29 + FXV C1-C6 |
| 镜像分发 | ✅ READY | export/import digest 一致 |
| Rollback | ✅ READY | 演练通过（多轮） |

### 未完成（需人工/操作员介入）

| 能力 | 状态 | 阻塞原因 |
|---|---|---|
| C 链（skill_case_retrieval） | ⛔ **BLOCKED** | ① approved model cache 缺失 ② provider metadata source/attestation 缺失 ③ RUN_BINDING_AUTH 密钥分发未闭合 |
| 生产 Fixer | ⛔ 未授权 | 需人工授权持久运行 |
| 生产 Verifier | ⛔ 未授权 | 需人工授权持久运行 |
| 真实 PR Fixer→Verifier 处理 | ⛔ 未授权 | 需真实仓库写权限（当前只读） |
| Registry push | ⛔ 未授权 | 需指定 registry |
| 生产部署 | ⛔ 未授权 | 需指定环境 |

### C 链阻塞详情（单独记录，不与 A 链混合放行）

```
RAG_CASE_RETRIEVAL = BLOCKED
embedding = DISABLED
pgvector = DISABLED
model_cache = DISABLED
RUN_BINDING_AUTH = NOT_WIRED
```

三缺口（全部需操作员行动）：
1. **approved model cache**：操作员须离线获取模型文件 + 生成含 SHA256 的 manifest + 署名批准
2. **provider metadata attestation**：部署 `case_provider_metadata` 行 + live 合同测试通过后置 `tests_attested=true`
3. **RUN_BINDING_AUTH key distribution**：密钥生成/分发/轮换/撤销机制落地

## 三 · 未完成项清单

| # | 项目 | 前置条件 | 阻塞者 |
|---|---|---|---|
| 1 | C 链启用 | 上述三缺口全部解决 | 操作员 |
| 2 | Fixer 持久 staging 运行 | 人工授权 | Owner |
| 3 | Verifier 持久 staging 运行 | 人工授权 | Owner |
| 4 | Fixer→Verifier 处理真实 PR | 授权 + 真实仓库写权限（当前只读） | Owner + GitHub |
| 5 | Registry push | 指定 registry + 凭据 | Owner |
| 6 | 生产部署 | 指定环境 + 基础设施 | Owner |

## 四 · 发现记录

| ID | 描述 | 严重度 | 影响 |
|---|---|---|---|
| FG-FB-01 | staging 密码 `pilot-read-only-2026` 硬编码于验证脚本（*.mjs） | P3 | 仅本机 staging（127.0.0.1）不可达外部；建议后续改用 env var |

## 五 · 最终授权请求

以下 12 项须逐项明确回复（沉默/模糊/仅查看 = 不构成授权）：

1. **Fixer 持久 staging 运行？** □ 允许 □ 不允许
2. **Verifier 持久 staging 运行？** □ 允许 □ 不允许
3. **Fixer→Verifier 处理真实 PR（只读克隆/隔离副本）？** □ 允许 □ 不允许
4. **新增用户？** □ 允许：＿＿ □ 不允许
5. **新增仓库/PR？** □ 允许：＿＿ □ 不允许
6. **真实 GitHub 写入？** □ 允许 □ 不允许
7. **A 链重新启用？** □ 允许 □ 保持关闭
8. **推进 C 链？** □ 允许（D7 下载授权） □ 保持 BLOCKED
9. **Registry push？** □ 允许：＿＿ □ 不允许
10. **生产部署？** □ 允许：＿＿ □ 不允许
11. **有效期** □ 7 天 □ 自定义：＿＿
12. **回滚版本**：sha256:41bd3029 + 既有手册；停止条件沿用

---

**判定：PROJECT_AUTONOMOUS_CLOSURE_AUTHORIZATION_READY**
