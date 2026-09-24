# D-A 解锁报告（2026-09-24 调查与修复轮）——**正式接线完成，PREFLIGHT PASS**

## 根因链（三层定位）
1. CRD `workers.agentteams.io/v1beta1` 声明 `spec.env`（16 缩进=spec 直接属性）——形状正确；
2. **`agt apply -f` CLI 解析缺陷**：apply 报 configured 但发送的 CR 不含 spec.env
   （kine 存储 spec=[containerManaged,image,model,runtime,state]，无 env）——丢弃发生在 CLI 写入路径；
3. controller 本身**支持**：安装版 223ddc2（2026-08-22）的
   `member_reconcile.go:923` 已调用 `mergeUserEnv(workerEnv, m.Spec.Env, ...)`，
   官方源码（agentscope-ai/AgentTeams main）确认 spec.Env→容器 env 合并路径
   （system-wins 保留前缀 AGENTTEAMS_*/OPENCLAW_*/HOME 等；MERGEPILOT_CR_* 无冲突）。

## 修复方式（k8s 原生 API，属 D-A 已批准的"正式 controller/Worker 环境注入"）
- 经 kube-apiserver（token.csv bearer + ca.crt，均未输出）PUT reviewer CR：
  `spec.env = {MERGEPILOT_CR_PG_DSN, MERGEPILOT_CR_REPO_SCOPE_FILE}`，model=deepseek-flash 保留；
- controller 哈希检测到 Env 变化 → **自动重建 reviewer 容器** → env 注入容器（计数=2）；
- scope file 经共享镜像固定路径 `shared/cr-scope/run-context.json`（gh_bridge authored，526B）。

## 生产 Preflight（reviewer 容器内，实际注入 env）：**PASS**
- 连接：case-pg 只读账号（elemiso-case-pg:5432/cases）
- 角色：非超极/非建权/非复制 ✓；`transaction_read_only=on` ✓
- 超时：statement_timeout=10000ms、lock_timeout=5000ms ✓
- 表能力：knowledge 12 必需列全 ✓；search_path=public ✓
- scope file：authored_by=gh_bridge、repo 正确 ✓

## 影响与边界
- skill_case_retrieval 在下一个真实 run 中将**可用**（此前诚实失败 SCOPE_MISSING）；
- rag_retrieve（BM25）不受影响；
- **本轮未执行 CASE2**（按指令"暂不执行 CASE2"），A1-A5 授权保留待用户放行；
- 回滚：apiserver PUT 删除 spec.env 两键 + 容器随 reconcile 重建即完全回滚（命令记录于 RPD）。

## 遗留缺陷登记（供上游）
`agt apply -f` 丢弃 CRD 已声明的 `spec.env` 字段——建议向上游 agentscope-ai/AgentTeams 报告
（复现：任意 worker manifest 含 spec.env → apply → kine/CR 无 env）。
