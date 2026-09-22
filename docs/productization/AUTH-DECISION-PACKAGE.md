# 授权决策包（AUTH-DECISION-PACKAGE）

**日期**：2026-09-22（M3.5 复核轮生成）｜ **性质**：决策支持，**不代替用户批准**。每项给出推荐、影响与回退；批复方式=对条目编号表态（批准/拒绝/改条件）。

## 一、外部授权项（R1–R7 合并表）

| # | 推荐 | 目标环境 | 操作 | 影响 | 验收证据 | 回退 | 所需权限 | 外部写/费用 |
|---|---|---|---|---|---|---|---|---|
| R1 桥故障注入 | **批准（建议）** | 本机桥进程 + 服务器 PG 台账（打标 `test-` 行，白名单仓库） | 注入测试投递行→起真桥→kill -9 桥三次（认领后/派发后/发布前）→重启观察接管 | 共享台账短暂出现 2-3 行测试行；本地栈跑一轮审查（既有模型预算内） | take_over_stale CAS 日志、resume 分流、旧 claim rowcount=0、台账终态导出 | 测试行 `DELETE WHERE test-`；桥进程即起即停 | 既有 SSH 通道；本地栈 | 无 GitHub 写；模型调用走既有额度 |
| R2 真实 GitHub 回写+对账 | **批准（建议）** | nghqqa/fastapi-boilerplate-demo 测试分支 PR + 服务器 reporter 容器 | 全链 POST check-run；人为断发布通道后恢复，验 reconcile/receipt | GitHub 可见 check-run（测试分支，主分支零影响）；App 配额数次 | POST 201+id、reconcile 采纳日志、receipt JSON、ERROR→恢复→PROCESSED 轨迹 | check-run 不可删——用 `mergepilot-test/*` 分支隔离；测试 PR 事后关闭 | 既有 App installation（checks:write） | **外部写入（GitHub）**；无费用 |
| R3 场景6 双 head | 批准（随 R2） | 同 R2 | 同 PR 推两个 head，各自 check-run 并存 | 2 个 check-run 可见 + 一轮重复审查 | 两 head 各自 check-run、台账两行 PROCESSED | 同 R2 | 仓库写权限（测试分支） | 外部写入 |
| R4 运行副本同步 | **批准（建议，先行）** | repo `tools/gh-bridge/`(+orchestrator/console_v3) → `D:\goai\r3work\scripts\` 等 | 备份 `.bak-<date>` → 复制 → 冒烟（off 模式行为不变→再开 shadow） | 现网桥获得 M1 加固+manifest+RAG 门+v3 hook（默认 off=行为不变） | 副本 sha256 一致；off 冒烟通过；shadow 证据落库 | `copy .bak → 原文件`（秒级） | 本机文件写入 | 无 |
| R5 worker 侧版本上报 | 暂缓（方案待定） | worker 镜像层（zz_agentloop hook / MCP server） | 只读探查已完成；镜像改动+重启 worker 需排期 | 改动会中断在途任务 | run-manifest missing[] 减少 | 镜像回滚 | 镜像重建+栈重启授权 | 无 |
| R6 usage 源 | **需用户二选一** | a) 外部 OTel collector；b) worker 本地台账（依赖 R5） | a) 只读查询 spans；b) 镜像层改动 | a) 零写入 | 单 run token 总数与 span 数自洽 | a) 无 | a) OTel 查询 key（已有） | a) 无费用；b) 同 R5 |
| R7 语料部署同步 | **批准（建议，先行）** | repo `tools/rag/corpus/` → `D:\goai\r3work\rag-live\`；启动 rag-live（:4184 本机） | 备份运行语料→corpus_tool.import（同内容零操作）→起服务→/healthz+快照核对 | 检索内容切到 repo 版本化语料；本机回环监听 | /healthz chunks=12；桥 manifest snapshot=corpus_tool 输出 | `copy .bak`；停服务=结束 node 进程 | 本机文件写入+本地进程 | 无 |

**建议顺序**：R4+R7（纯本地、低风险、先行）→ R1+R2 合并真实案例轮（v3 shadow 随轮产出对照证据）→ R3 附属于该轮 → R5/R6 按用户选路。

## 二、独立决策项（不替用户决定，仅列选项与影响）

| # | 决策 | 选项 | 影响 |
|---|---|---|---|
| D-1 | V0 启用审批动作集 | generate_patch / run_poc / publish_result 的子集 | 机制已实现且拒绝未配置状态；拍板后门页才能出现对应动作 |
| D-2 | 审批人身份与权限映射 | 需至少一名具名审批人 + 仓库/动作范围 | approved_by 非空已强制；无映射前真实审批不可启用 |
| D-3 | 审批/执行 TTL 默认值 | 沿用 l2 惯例 1..24h 或自定 | 参数已化；默认值=产品政策 |
| 预算金额 | 单 run / 全局硬预算数值 | 数值+币种或 token 上限 | BudgetGuard 已就绪；未配置不产生任何消费授权 |
| R6 路线 | a) OTel 查询 vs b) 本地台账 | 见上表 | 影响成本计量的 token 面何时闭合 |
| 密钥轮换 | 恢复执行时间（当前挂起） | 按 secrets-locations 四级联 | **M4 出口条件**；挂起≠验收通过 |

## 三、可先本地执行（无需新授权，已完成或可继续）

- v3 shadow/fixture 链路、只读控制台、RunStore——已完成并有 200 项测试；
- 门 CLI、票据存储、预算守卫逻辑层——已完成；
- shadow 证据导出到证据包、控制台页面打磨——可继续，均不触外部。

## 四、本轮不可逆操作清点

无。本轮未执行 push、运行副本同步、GitHub 写入、共享环境变更、密钥操作。
