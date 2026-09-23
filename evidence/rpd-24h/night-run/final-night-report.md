# 夜轮最终报告（2026-09-24，收口）

## 最终状态：**BLOCKED**（D-A 接线被 AgentTeams 官方接口能力阻塞，按规则停止进入 CASE2）

## 执行链
| 阶段 | 结果 |
|---|---|
| NR-01 push（D-E） | ✅ DONE：a14ce1d→5c2312f 推送成功（gh 通道），PR #233 head 核验一致 |
| NR-02 D-A 接线 | ⛔ **BLOCKED**：只读账号✓、scope file 镜像✓、DB 侧 preflight exit 0；**controller 静默丢弃 spec.env**（kine 存储证明） |
| NR-03 A1 空提交 | ⛔ BLOCKED（依赖 D-A，按 §七 停止进入 CASE2） |
| NR-04 A2/A3 真实 run | ⛔ BLOCKED（同上；零模型调用、零 rag-live 启动） |
| NR-05 人工门停止+报告 | ✅ DONE（无 ticket 产生，D-D 维持 WAITING_FOR_CASE2_TICKET） |

## 18 项速览
1. PR #233 最终 head：**5c2312f**（OPEN，MERGEABLE）
2-4. CASE2/空提交/真实模型调用：**均未发生**（D-A BLOCKED 前置停止）
5. 模型：容器保持 deepseek-flash（未变）
6. 请求数/token：0/0（CASE2 未启动）
7. rag-live：未启动
8-9. rag_retrieve/检索结果：未发生
10-11. ticket：未创建（D-D=WAITING_FOR_CASE2_TICKET）
12. check-run：0
13. approve/reject/fixer/verifier：均未发生
14. 共享 case-pg：仅只读账号口令配置（D-A 批准项），零业务数据
15. controller/Worker：CR env 注入尝试被 controller 丢弃（CR 实质未变，model 保持）；未手工改容器文件
16. evidence：evidence/rpd-24h/{night-run/,D-A/,pr-audit/,rpd-01..08/}
17. 失败/重试：NR-02 一次定性失败（非重试型）；盲目重试=0
18. 回滚点：PR 层=a14ce1d 仍可 closed/deleted；本地=git reset --hard b79cd39；运行副本回滚点=r3work/rollback-20260923-222605/

## 明早用户的唯一决策（D-A 解锁三选一）
1. **升级/修补 AgentTeams controller** 使 `spec.env` 生效（厂商/版本路径）；
2. 允许 **ctrl compose env 模板注入**（改 compose + 重启 elemiso-ctrl——影响面：Matrix/MinIO/gateway 短暂中断）；
3. 允许 **镜像内置 env 默认值**（重建 copaw-worker 镜像，把 MERGEPILOT_CR_* 设为构建默认）。
D-A 解锁后，CASE2 A1-A5 的授权仍然有效，可随时按 RUNBOOK 执行。
