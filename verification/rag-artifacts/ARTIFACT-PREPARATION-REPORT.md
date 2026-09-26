# RAG_ARTIFACT_PREPARATION_ONLY — 工件准备与核验报告

基线：console @ d12c2d8（未回退） · 隔离 PG（mp-cc-pg）/ 无共享资源 / 零启用
**状态维持：RAG registration=DISABLED · embedding 未下载未生成 · RUN_BINDING_AUTH=NOT_WIRED**

## 1 · approved model cache — ❌ 工件缺失（模板与校验链已就绪）

已准备：
- `approved-cache-manifest.template.json`：全字段模板（model/version/dimension/files
  [path+bytes+content_sha256]/source/approver[署名+node_id+批准依据]/validity[not_before+
  expires_at 到期 fail-closed]/cache_path_env）——字段名对齐 verify_approved_cache 实际
  schema（content_sha256）
- 校验链实测：模板占位值被**正确拒绝**（ApprovedCacheError: missing cache file）——
  fail-closed 机制在真实模板上工作

缺失（fail-closed 如实记录）：
- **无已批准的模型缓存实例**。embedding 下载保持 DISABLED（唯一下载通道为 D7 单独授权，
  本轮未申请）；模型文件与署名批准的 manifest 须操作员按 RUNBOOK A 提供

## 2 · case_provider_metadata — ⚙️ 迁移与失效行为已就绪；attestation 未闭合

已准备并核验（隔离 PG：mp-cc-pg）：
- `ensure_schema` 应用成功，表结构核验通过（id/authored_by/schema_name/table_name/
  snapshot_id/snapshot_digest[64-hex]/row_count/embedding_model/embedding_version/
  dimension/tests_attested 默认 FALSE/updated_at）
- authoritative source（PgAuthoritativeMetadataSource）读取正常，is_authoritative=True
- **失效行为矩阵实测**：空表→REGISTRATION_NO_SOURCE；写入未 attest 行→
  REGISTRATION_TESTS_NOT_ATTESTED；query↔store 不对齐→REGISTRATION_NOT_ALIGNED——
  全部拒绝，无静默放行

缺失：
- **tests_attested=true 的真实 attest**（需 live 合同测试在真实部署上通过后由后端置位；
  当前行为占位行，model=PLACEHOLDER-MODEL，明确非真实）

## 3 · RUN_BINDING_AUTH — ⚙️ 合同矩阵完备；密钥分发未闭合 → 保持 NOT_WIRED

已准备并核验：
- `run-binding-key.template.json`：key_id/key_version/algorithm（HMAC-SHA256 full）/
  created_at/rotate_by/status=PENDING_DISTRIBUTION/distribution/revocation（撤销清单）
- `RUNBOOK.md` C 节：生成→分发→轮换（rotate_by 到期换 version）→撤销流程
- 合同矩阵复验 **26/26**（tests/rag_live/test_rag_contract.py）：nonce 一次性（重放 400）、
  篡改 head 400、跨 query 400、错 key 400、required 未绑定 409、审计 redaction、
  默认 off 普通头（含伪造签名形状）一律 unbound

缺失：
- **密钥分发机制未落地**（生产密钥管理/轮换执行/撤销清单）——按约束保持 NOT_WIRED，
  无降级放行

## 复验：core control plane 不受影响

- 两个授权 PR 只读路径正常（/api/pulls 与 /api/overview 的 PR 集合完全一致：
  nghqqa/tizhou#2 + wookat/speaktype#426）
- /overview 阶段数据与 /pending 一致（pending_summary.count == len(/api/pending)）
- 阶段全部来自后端权威枚举（7 值），stage_counts 总和 == prs 行数；
  BLOCKED 行携带 stage_source（integrity/gate 出处）——**前端不改写阶段**
  （人话标签仅为展示映射，STAGE_LABEL 一一对应无合并/别名）

## 判定

**RAG_ARTIFACTS_BLOCKED**

阻塞项（进入独立 RAG Canary 前）：
1. 操作员提供署名批准的 model cache manifest + 模型文件（不下载则 Canary 无法做真实检索）
2. case_provider_metadata 写入真实 provider 元数据并完成 live 合同测试 attest
3. RUN_BINDING_AUTH 密钥分发机制落地（轮换+撤销可执行）

状态维持声明：RAG=EXCLUDED/DISABLED · embedding=DISABLED · RUN_BINDING_AUTH=NOT_WIRED ·
无 GitHub 写入 · Fixer/Verifier 未启动 · 用户/仓库/PR 范围未扩大。
