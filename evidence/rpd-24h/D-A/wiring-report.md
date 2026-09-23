# D-A 正式接线报告（2026-09-24 夜轮）——结果：**BLOCKED（AgentTeams 官方接口不足）**

## 已完成（授权范围内）
1. **case-pg 只读账号**：`case_retrieval_reader` 已设随机口令（存本地 r3work/model-switch/cr-reader.pw，600，不入 repo）；
   knowledge 表 repo_scope/embedding 列确认存在（finals 迁移已应用）。**未改任何业务数据**。
2. **scope file**：gh_bridge `build_run_context` 生成的 run-context.json（authored_by=gh_bridge）
   已写入共享镜像固定路径 `teams/elemiso-team/shared/cr-scope/run-context.json`
   → 容器内 `/root/.copaw-worker/reviewer/shared/cr-scope/run-context.json` 实测存在（526B）。
3. **Worker CR env 注入尝试**：`agt apply -f`（apiVersion agentteams.io/v1beta1，spec.model=deepseek-flash 保留 +
   spec.env 两个 MERGEPILOT_CR_* 变量）→ 返回 "worker/reviewer configured"，
   但 **kine 中存储的 CR spec 仅 [containerManaged,image,model,runtime,state]，env 被静默丢弃**（stored-cr-proof.txt）。
4. **DB 侧验证（host 经 127.0.0.1:15432）**：validate_env --preflight **exit 0**
   （连接+只读角色+表能力+scope file 可信全过）；错误库负向=exit 5 脱敏。
5. **Worker 状态恢复**：sleep→wake 循环后 reviewer Running、model=deepseek-flash 保持。

## BLOCKED 根因
CRD `workers.agentteams.io.yaml` 声明了 `spec.env`（用户自定义环境变量注入），但当前 controller 实现
**不支持**：apply 后 env 既不入 CR 也不入容器（容器内 MERGEPILOT_CR_* 计数=0）。
替代通道均超出本轮禁止项（改 ctrl compose+重启 controller / 手工改容器文件 / 重建容器）。
→ 按 §七"AgentTeams 官方接口不足"= 人工介入条件，**D-A=BLOCKED，停止进入 CASE2**。

## 影响
- CASE2 的 skill_case_retrieval 将继续诚实失败（SCOPE_MISSING/DB_UNAVAILABLE）——与 CASE1/事实一致；
- rag_retrieve（BM25 rag-live）**不依赖 D-A**，未来获批 run 仍可消费；
- 后续解锁路径（需用户/厂商动作）：升级 AgentTeams controller 支持 env、或在其 manifest 中持久化 env、
  或采用镜像内置 env 默认值方案。

## 回滚/清理状态
- 共享 case-pg：仅口令配置（D-A 批准项）；零业务数据变更
- 共享 MinIO：新增 cr-scope/run-context.json（桥 authored，无害，供未来接线）
- Worker CR：未变（env 被丢弃，model=flash 保持）；reviewer 容器 Running
- 本地机密文件：cr-reader.pw / reviewer-cr.yaml（含 DSN）均 600 权限、位于 repo 外
