# RAG_CONTROLLED_PREFLIGHT — 就绪度检查报告

基线：console @ 714e6c2（未回退）；组件事实来源 integration @ 08dbe35 树（只读检查）
本轮性质：**只读就绪度检查**——零启用、零下载、零注册、零共享资源；
隔离探针（helpers/preflight-probes.py）+ 本地 fixture 合同测试。

**终态维持：RAG=EXCLUDED · embedding=DISABLED · RUN_BINDING_AUTH=NOT_WIRED**

## 逐项核证

### 1 · approved model cache — ❌ 不具备（fail-closed 已验证）
- 机制在位：`approved_cache.verify_approved_cache`（manifest 需 model/version/dimension/
  files+SHA256；为审批工件，env `MERGEPILOT_CR_APPROVED_MANIFEST` 指定）+
  `embedding/offline_gate`（`local_files_only=True`、子进程 `HF_HUB_OFFLINE=1`、
  下载仅凭 `MERGEPILOT_D7_EMBEDDING_DOWNLOAD_ALLOWED=1` 单独授权）
- 实测（preflight-probes.py）：真实默认缓存定位 → **None（本机无已批准缓存）**；
  空缓存目录定位 → None；`download_authorized(empty env)=False`；
  manifest 缺失 → `FileNotFoundError`；哈希不符 → `ApprovedCacheError`（全 fail-closed ✓）
- **缺口：不存在任何经批准的模型缓存与清单**——需操作员提供（属部署/审批工件）

### 2 · provider metadata 权威源 — ❌ 未部署（接口在位、决策 fail-closed）
- 机制在位：`metadata_registry.registration_decision`（无源→REGISTRATION_NO_SOURCE；
  非权威源拒绝；store↔query 未对齐拒绝；`tests_complete` 未 attest 拒绝）+
  `PgAuthoritativeMetadataSource`（PG 表 `case_provider_metadata`，含 snapshot_digest/
  row_count/tests_attested，读-only，ensure_schema 幂等）
- 实测：无源 `allow=False REGISTRATION_NO_SOURCE` ✓；
  隔离 staging PG 上 `case_provider_metadata` 表 **present=0（未部署）**
- **缺口：无任何部署接线该表并完成 live 合同测试 attest**

### 3 · RUN_BINDING_AUTH — ⚙️ 接口完备、部署 NOT_WIRED（正确保持，无降级）
- 合同测试 26/26 通过（tests/rag_live/test_rag_contract.py，本地 fixture 零网络）：
  默认 off 普通头（含任意伪造签名形状）一律 unbound；单 run 文件绑定需显式
  single-run；HMAC 全矩阵（合法→bound / 同签名重放→400 / 篡改 head→400 /
  换 query→400 / 错密钥→400）；required 模式 unbound→409 fail-closed；
  重试/幂等合同；审计记录绑定状态与 redaction
- **缺口：密钥分发未闭合**（无生产密钥管理/轮换）；按约束 3 保持 NOT_WIRED ✓

### 4 · run/head/repo 绑定校验 — ✅ 具备且久经实测
stale head → `SKILL_GATE_REFUSED_STALE`；跨仓回执喂他 run → REFUSE；绑定字段仅取
自信任 run-context——本轮会话 canary4/5/6 与 user-pilot U4 连续实测在案。

### 5 · 缺失即 fail-closed — ✅ 全部实测
无 cache ✓ · manifest 缺失/哈希不符 ✓ · 无元数据源 ✓ · required 未绑定 409 ✓ ·
绑定冲突（stale/跨仓/篡改）✓

### 6 · core control plane 不受影响 — ✅
console health 200；登录后 /api/pulls POSTGRESQL_LIVE（回滚后新库诚实空数组）；
本轮全程未触碰核心面组件。

### 7 · 进入独立 RAG Canary 的条件 — ❌ 尚不具备
阻塞项（全部为**部署/审批工件**，代码接口均已就位并有合同测试）：
1. 操作员提供 approved model cache 清单 + 缓存（文件/维度/SHA256/来源）
2. 部署 PG `case_provider_metadata` 并完成 live 合同测试 attest
3. RUN_BINDING_AUTH 密钥分发机制落地（生产密钥管理/轮换/撤销）

## 判定

**RAG_PREFLIGHT_BLOCKED**（前置条件未全部具备；所有缺失路径均已实测 fail-closed，
无静默放行）

维持声明：RAG=EXCLUDED · embedding=DISABLED · RUN_BINDING_AUTH=NOT_WIRED。
不宣称：RAG 已接入 · 生产上线 · 全量内测就绪 · GitHub 写入开启。
