# CASE2-B 真实运行报告（2026-09-24 ~00:32-00:52 UTC）

## 执行事实
- 旧 head `254f61ce`（前次 CASE2，delivery=ERROR/TIMEOUT 终态）→ 新 head **`42ed1787`**（单一空提交）
- delivery `2432e0f0` → 唯一认领 run **`run-gh-pr2-42ed1787-003205`**（attempt 1）
- 模型：leader+reviewer = **deepseek-flash**（真实调用）；catalog_state_at_dispatch=[flash, v4-pro]
- manifest `172e744aa9b7`、run-context 八字段齐（missing=[]）、rag snapshot `fd34c304` 绑定、
  `service_state_at_dispatch=reachable`（**上轮探测修复在生产生效**）

## RAG 消费（本轮主目标）——**真实消费达成（第二次）**
- `rag_retrieve` ×2（00:32:54.136/.143），result_status=OK，document_count=3，
  source_refs=`org-standards/cwe-22-path-traversal.md#1` + `org-standards/file-path-containment.md#1`
- 审查员 findings **明确引用**两条组织标准作为 HIGH 定级佐证（与 CASE2 首次一致）
- retrieval_mode=lexical-zh-en-v1（BM25）；零 embedding 下载（D-7 合规）

## case_retrieval（D-A 接线验证）
- **仍失败：SCOPE_MISSING**——失败分类如实记录、fail-closed、零回退。
  根因（已定位非阻塞）：**worker 镜像内 core.py 无 `MERGEPILOT_CR_REPO_SCOPE_FILE` 回退**
  （repo 代码已具备该特性，镜像同步待办）；env 注入本身工作正常。
- 不影响本轮主目标；正式可用需镜像同步（待办已登记）。

## 终态
- 20min 截止自然收口：**leader 未写 gate marker**（kickoff 契约已含指令，deepseek-flash leader
  未执行文件写入动作——已知模型指令遵循局限）→ verdict=timeout
- check-run：**107446711189**（mergepilot/review，neutral，app=mergepilot-reporter，head=42ed1787，
  receipt adopted=false）——唯一一个
- delivery=ERROR `TIMEOUT(manual) timeout; publish=ok`
- **ticket 未创建**（marker 契约未执行；按契约不补造、不伪造）→ D-D 维持 WAITING_FOR_CASE2_TICKET
- rag-live 已停止；worker 恢复 Running；业务仓库克隆零污染

## 人工决策（与 CASE2 首次相同）
HIGH finding（CWE-22 任意文件读）真实存在且证据充分——等待用户决策（批准→fix 路线 / 拒绝→blocked）。
注：正式 ticket 需 leader 写 marker；该模型行为局限已记录，可考虑后续轮在桥侧对
"HIGH 已确认且超时"路径按结构化证据补建 ticket（需用户新授权，本轮未做）。
