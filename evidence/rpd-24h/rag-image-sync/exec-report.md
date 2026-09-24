# RAG-IMAGE-SYNC 执行报告（2026-09-24）——**DONE，容器内验收全过**

## 变更
- 新镜像：`agentteams/copaw-worker:223ddc2-agentloop-v6scope`（digest 前缀 f1702d75675c）
  - 构建方式：`FROM 223ddc2-agentloop-v5fix` + `COPY case_retrieval/core.py`（单文件增量，构造上只含批准变更）
  - 构建上下文敏感扫描：0 命中（仅 core.py + Dockerfile 两个文件）
- Worker 更新：`agt update worker --name reviewer --image …v6scope`（官方 CLI；"worker/reviewer configured"）
- CR 复核：image=v6scope ✓、**model=deepseek-flash 保留** ✓、spec.env 两个 MERGEPILOT_CR_* 保留 ✓
- 容器：controller reconcile 已重建，运行于新镜像

## 容器内验收（全部 PASS）
1. 新镜像生效（container Config.Image = v6scope；core.py = repo 版 e754551b…）
2. env×2 存在（值未打印）；scope file 存在（526B，authored_by=gh_bridge，repo 正确）
3. 正式 preflight：连接 + 只读角色（非超极/readonly=on）+ 表能力 ✓
4. **有效 scope 查询（真实共享 case-pg）**：total_found=4 / returned=3 / knowledge_base_size=7 /
   repo_scope 正确 / latency=107ms，命中 PR #2 knowledge 条目（citation.source_url）
5. **负向**：scope 缺失 → `CASE_RETR_SCOPE_MISSING`（fail-closed；无全库回退——适配器 WHERE repo_scope 起始）

## 边界声明
- 零业务数据写入（全部只读会话）；零模型调用；零 rag-live；零 check-run；零 AgentTeams 内部数据库变更
- 未执行真实 approve/reject；未派发 fixer/verifier；未执行 CASE2（本批不含 A1-A5）

## 回滚
`agt update worker --name reviewer --image agentteams/copaw-worker:223ddc2-agentloop-v5fix`
+ 等 reconcile + 核对旧 digest 4bfe8eccfe4f 与 env/model。
