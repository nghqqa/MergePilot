# 最终决策／授权表（FINAL DECISION TABLE）

**日期**：2026-09-22（授权前验证关口）｜ **批复方式**：逐条标注"批准/拒绝/改条件"，回复编号即可。
本表是唯一待批清单；此前 AUTH-DECISION-PACKAGE 的分析与回退细节保留在文件后半部分作为支撑材料。

## A. 需要用户决定（产品政策——拍板前机制保持关闭）

| # | 事项 | 推荐选项 | 精确操作 | 影响 | 验收证据 | 回退方式 | 外部写/费用 |
|---|---|---|---|---|---|---|---|
| D-1 | 启用哪些审批动作 | `generate_patch` + `publish_result`（暂缓 run_poc） | 在 adapter/config 声明启用子集（一处常量） | 门页出现对应动作；未列动作继续拒绝 | 门页动作清单与配置一致；未启用动作构造请求被拒 | 改回空集即关闭 | 无（未接真实执行前无副作用） |
| D-2 | 审批人映射 | 内测期单审批人（GitHub 用户名）+ 白名单仓库范围 | 在 RunStore/门配置写入 mapping | approved_by 对照 mapping 校验生效 | 非映射人 approve 被拒的测试记录 | 清空 mapping=回到拒绝全部 | 无 |
| D-3 | 审批 TTL | approval 24h / 执行 1h（沿用 l2 惯例） | 配置默认值 | 过期票自动 EXPIRED | 过期拒绝的测试已存在（test_expired_*） | 改参数 | 无 |
| D-4 | 预算金额 | 建议：单 run token 上限（数值待定），先只读计量一周再定硬限 | 设置 MERGEPILOT_RUN_BUDGET_TOKENS（已备 factory：未设置=不检查） | 设置后 dispatch 超预算即拒 | hooks 工厂测试（超限阻断+台账恢复） | 取消 env=回到只读计量 | 无直接费用；防的是失控费用 |
| D-5 | R6 token 计量路线 | a) OTel collector 查询（零镜像改动） | 选 a 或 b；a 需提供 collector 查询方式 | token/币值面闭合时间 | 单 run token 总数与 span 数自洽 | a) 无回退需要 | a) 只读查询；b) 同 R5 |
| D-6 | 密钥轮换恢复 | 建议：R2 批复同批恢复（真实 GitHub 写前轮换） | 按 secrets-locations 四级联执行 | 凭证更新，旧凭证失效 | 轮换后 receiver/reporter 健康检查 | 旧凭证暂时并存回滚 | 无直接费用 |

## B. 需要用户授权（外部操作——逐项批复后才执行）

| # | 事项 | 推荐 | 精确操作 | 影响 | 验收证据 | 回退方式 | 外部写/费用 |
|---|---|---|---|---|---|---|---|
| R4 | 同步运行副本 | **批准（建议先行）** | 备份 r3work 副本(.bak-日期)→校验 sha256→复制 bridge+tools 子集→off 冒烟；**保持 MERGEPILOT_REVIEW_V3=off** | 现网桥获得 M1 加固+manifest+RAG 门+v3 hook（off=行为不变） | 副本 sha256==repo；off 冒烟台账正常 | `copy .bak→原文件`（秒级） | 无 |
| R7 | 同步语料+启动 rag-live | **批准（建议先行）** | 备份运行语料→corpus_tool.import（同内容零操作）→node 启动 :4184（本机回环）→快照核对 | 检索切到 repo 版本化语料；RAG snapshot 可绑定 | /healthz chunks=12+corpus_file_sha256；manifest snapshot 一致 | `copy .bak`；结束 node 进程 | 无 |
| R1 | 真实故障恢复验证 | 批准（随第二阶段） | 服务器 PG 注入 `test-` 打标行→真桥 kill -9 三阶段（认领后/派发后/发布前）→重启接管 | 共享台账短暂出现测试行；真实模型调用（既有额度） | take_over_stale CAS、resume 不重发、对账收敛、逐场景独立判据 | 测试行 DELETE；桥进程即起即停 | 无 GitHub 写；模型费用=既有额度 |
| R2 | 真实 Agent/RAG 案例 | 批准（第二阶段，R1 之后） | 白名单测试分支 PR：先 v3 shadow→再真实 Agent 全链→发布故障恢复演练 | GitHub 测试分支可见 check-run；一轮完整审查费用 | POST 201+id；result.md 引用 RAG source_ref；RunStore 记录；控制台展示 | 测试分支隔离，主分支零影响；PR 事后关闭 | **外部写入（GitHub）**；真实模型费用 |
| R3 | 真实 GitHub 回写矩阵 | 批准（第三阶段，R2 之后） | 回写成功/重复 webhook/PR 更新双 head/回写失败恢复/旧 run 失效 五场景逐一 | 同 R2 | 每场景独立判据（不合并判定）；双 head 并存 check-run | 同 R2 | **外部写入（GitHub）**；模型费用 |

**授权后执行顺序（获批即按此执行，不再询问）**：
- 第一阶段：R4 + R7（备份→校验→同步→off 冒烟→记录回退命令；保持 v3=off）
- 第二阶段：R1 + R2（测试仓库/PR → 先 shadow → 再真实 Agent → 逐场景独立判据；不自动批准/合并/推代码）
- 第三阶段：R3（回写矩阵五场景）

**启用规则（铁律）**：未获得 R2 的真实验证证据前——`MERGEPILOT_REVIEW_V3` 不切 on、不宣称 M1/M3/M4 通过、不启用真实审批、不改旧串行链默认行为。真实验证失败：保留 shadow、记录证据、最小修复、重跑受影响场景，不降低断言。

## C. 本地已完成（无需授权，本轮产出）

- `tools/integration_prep/`：R1/R2/R3 执行计划（声明式步骤+证据点）、R4/R7 同步计划（备份/校验/同步/冒烟/回退）、证据采集器（集中脱敏：GitHub token/DSN/API key/OTel key）、授权闸门（默认 dry-run，执行需 `MERGEPILOT_IT_AUTH=1` + 批复）；
- rag-live 事实源（tools/rag/live/）：/health 暴露 `corpus_file_sha256`（部署核对）；search 接受可选 `run_id` 并落审计（R5 接通后 run 关联即闭合；向后兼容）；
- 预算接入点：`tools/costmeter/hooks.py` 工厂——`MERGEPILOT_RUN_BUDGET_TOKENS` 设置即 fail-closed（超限拒+台账崩溃恢复），未设置返回 None（现状语义：不检查、不产生授权）；
- v3 shadow 记录 ↔ 控制台读模型一致性：既有测试逐字段断言 + 冒烟核验（前轮）。

---

# 支撑材料（前版分析，保留备查）

## 一、外部授权项分析（R1–R7 详情）

| # | 推荐 | 目标环境 | 操作 | 影响 | 验收证据 | 回退 | 所需权限 | 外部写/费用 |
|---|---|---|---|---|---|---|---|---|
| R1 桥故障注入 | 批准（建议） | 本机桥进程 + 服务器 PG 台账（打标 `test-` 行，白名单仓库） | 注入测试投递行→起真桥→kill -9 桥三次（认领后/派发后/发布前）→重启观察接管 | 共享台账短暂出现 2-3 行测试行；本地栈跑一轮审查（既有模型预算内） | take_over_stale CAS 日志、resume 分流、旧 claim rowcount=0、台账终态导出 | 测试行 `DELETE WHERE test-`；桥进程即起即停 | 既有 SSH 通道；本地栈 | 无 GitHub 写；模型调用走既有额度 |
| R2 真实 GitHub 回写+对账 | 批准（建议） | nghqqa/fastapi-boilerplate-demo 测试分支 PR + 服务器 reporter 容器 | 全链 POST check-run；人为断发布通道后恢复，验 reconcile/receipt | GitHub 可见 check-run（测试分支，主分支零影响）；App 配额数次 | POST 201+id、reconcile 采纳日志、receipt JSON、ERROR→恢复→PROCESSED 轨迹 | check-run 不可删——用 `mergepilot-test/*` 分支隔离；测试 PR 事后关闭 | 既有 App installation（checks:write） | **外部写入（GitHub）**；无费用 |
| R3 场景6 双 head | 批准（随 R2） | 同 R2 | 同 PR 推两个 head，各自 check-run 并存 | 2 个 check-run 可见 + 一轮重复审查 | 两 head 各自 check-run、台账两行 PROCESSED | 同 R2 | 仓库写权限（测试分支） | 外部写入 |
| R4 运行副本同步 | 批准（建议，先行） | repo `tools/gh-bridge/`(+tools 子集) → `D:\goai\r3work\scripts\` | 备份→校验→复制→off 冒烟 | 现网桥获得全部加固（默认 off=行为不变） | 副本 sha256 一致；off 冒烟通过 | `copy .bak`（秒级） | 本机文件写入 | 无 |
| R5 worker 侧上报 | 暂缓 | worker 镜像层 | 只读探查已完成；镜像改动需排期 | 中断在途任务 | missing[] 减少 | 镜像回滚 | 镜像重建授权 | 无 |
| R6 usage 源 | 需选路 | OTel collector 或 worker 本地台账 | 查询或镜像改动 | token 面闭合 | token 与 span 数自洽 | a) 无 | OTel key（已有） | a) 无 |
| R7 语料同步 | 批准（建议，先行） | repo corpus → r3work/rag-live + 启动服务 | 备份→import→启动→核对 | 检索切版本化语料 | /healthz+snapshot 一致 | `copy .bak`；停进程 | 本机写入+本地进程 | 无 |

## 二、独立决策项详情

见上表 A 节（已合并；历史选项分析：审批动作=规格 §1 表；审批人=规格 §6 D-2；TTL=l2 惯例 1..24h；预算=costmeter 已就绪待数值；R6=两路线成本见 ACCEPTANCE-COST；密钥轮换=DECISIONS #5）。

## 三、本轮不可逆操作清点

无。未 push、未同步运行副本、未 GitHub 写入、未共享环境变更、未密钥操作。
