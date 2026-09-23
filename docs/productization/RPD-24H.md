# RPD-24H 展示视图（24h 自主交付轮）

**轮次**: RPD-24H-20260923 ｜ **开始**: 2026-09-23T15:09:04Z ｜ **BASE_HEAD**: `b79cd39` ｜ **最终 HEAD**: `05d6d40`
**分支**: `feat/rpd-24h-delivery`（PR #233 → main） ｜ 状态源: [RPD-24H.yaml](RPD-24H.yaml)

## 任务板

| ID | 任务 | 状态 | 优先级 | 证据 | 备注 |
|---|---|---|---|---|---|
| RPD-01 | 启动门禁和基线记录 | **DONE** | P0 | [startup-report](../evidence/rpd-24h/rpd-01/startup-report.md) | BASE_HEAD 复现✓ 工作树干净✓ 无秘密✓ |
| RPD-02 | pgvector 隔离证据复核 | **DONE** | P0 | [reverify](../evidence/rpd-24h/rpd-02/reverify.md) | 新一次性实例重放 11/11,实例已销毁 |
| RPD-03 | case_retrieval 接线契约 | TODO | P1 | — | 共享注入需要时→WAITING_HUMAN |
| RPD-04 | TicketStore 和人工门闭环 | TODO | P1 | — | 禁真实决策/派发 |
| RPD-05 | 后端回归 | TODO | P0 | — | 保留 TEST-DEBT 口径 |
| RPD-06 | 有限前端契约对齐 | TODO | P2 | — | 仅八种门状态展示 |
| RPD-07 | PR 交付 | **DONE** | P0 | [pr-record](../evidence/rpd-24h/rpd-07/pr-record.md) | **PR #233** OPEN(核验) |
| RPD-08 | 最终报告 | **DONE** | P0 | [final-report](../evidence/rpd-24h/rpd-08/final-report.md) | **MANUAL-REQUIRED** |

## 进度快照（每 30 分钟追加）

| 时间(UTC) | 当前任务 | commit | 测试 | 请求数/token | 阻塞 | 下一步 |
|---|---|---|---|---|---|---|
| 15:09 | RPD-01 | b79cd39 | — | 0/0 | 无 | 门禁+RPD 初始化 |
| 15:14 | RPD-02 | b79cd39 | — | 0/0 | 无 | pgvector 证据复核 |
| 15:30 | RPD-03 | b79cd39 | smoke 重放 11/11 | 0/0 | 无 | case_retrieval 接线契约 |

## PR 范围审计（2026-09-24 复核轮）

**PR_SCOPE_ACCEPTABLE = true**：157/157 文件全部归属 R3..RPD-08 各轮（后端 74/测试 45/文档 19/RPD 证据 17/仓库配置 2，+19013/−67）；
零无关文件、零生成物、零 .env/私钥、零生产配置、零共享数据、秘密扫描净（3 处命中=脱敏测试夹具）。
拆分方案 A（保留综合 PR，推荐）/ B（四段拆分）待用户选择——详见 [pr-scope-report](../evidence/rpd-24h/pr-audit/pr-scope-report.md)。

## 执行判定（2026-09-24 执行指令轮）

指令要求"按已批准子项继续执行"，但 **RPD user_reply 五项全空、指令与仓库均无"批准 D-X"明确形式**
→ 判定=**批复缺失**：保持 MANUAL-REQUIRED，零外部动作（未 push db62d1c/9dcf3f6、未注入环境、未启用审批、未执行 CASE2）。
等待明确批复：`批准 D-A` / `批准 D-B` / `批准 D-C A1-A5` / `批准 D-E` / `修改条件：…`。

## 人工决策登记（2026-09-23 复核轮）

| decision_id | 内容 | 状态 | 用户答复 |
|---|---|---|---|
| D-A | controller/Worker 环境注入 + case-pg 只读账号 + Worker 重启 | **WAITING_HUMAN** | 无 |
| D-B | D-1/D-2 真实审批（动作子集/具名审批人/24h TTL/真实决策面/仅出计划） | **WAITING_HUMAN** | 无 |
| D-C | CASE2 运行授权（A1 空提交 / A2 flash 审查 / A3 rag-live / A4 单 check-run / A5 费用风险） | **WAITING_HUMAN** | 无 |
| D-D | 具体票据的人工门决策 | **WAITING_FOR_CASE2_TICKET** | 无（票据未产生） |
| D-E | gh CLI 凭据通道（限本仓库 feature branch/禁输出 token/禁 push main/禁 merge） | **WAITING_HUMAN** | 无 |

批准格式：`批准 D-A` / `批准 D-C A1-A5` / `修改条件：…`；模糊表达（"继续/可以/开始"）不构成批准。
部分批准只执行对应范围，其余保持 WAITING_HUMAN，整体状态保持 MANUAL-REQUIRED。
D-E 明确决定前：**零新的 GitHub 写入**（本复核轮的 RPD 更新仅本地提交，不推送）。

## D-A 解锁（2026-09-24 调查修复轮）：**正式接线完成，PRODUCTION PREFLIGHT PASS**

根因=**agt apply CLI 丢弃 spec.env**（controller 本身支持：223ddc2 `member_reconcile.go:923` 调用
`mergeUserEnv(workerEnv, m.Spec.Env, ...)`）。修复=经 kube-apiserver 原生 PUT 写入 spec.env 两键
（D-A 已批准的注入内容），controller 检测 Env 变化自动重建 reviewer 容器，env 注入实测=2，
容器内 preflight 全绿（只读角色/超时/表能力/scope 信任）。**本轮按指令未执行 CASE2**；
A1-A5 授权保留。回滚=apiserver PUT 移除 spec.env 两键+容器重建。

## 夜轮结果（2026-09-24 收口）：**BLOCKED**

NR-01 push ✅（PR #233 @ 5c2312f）；NR-02 D-A 接线 ⛔ **BLOCKED**——controller 不实现 CRD 已声明的
`spec.env`（apply 报 configured 但 kine 存储被静默丢弃，容器 env 计数=0；DB 侧接线本身已验证 exit 0）。
按 §七"AgentTeams 官方接口不足"停止进入 CASE2：NR-03/04 BLOCKED，**CASE2 未执行、零模型调用、零 ticket**。
解锁三选一：①升级 controller 支持 env ②ctrl compose env 模板注入（需重启 elemiso-ctrl）③镜像内置 env 默认值。
D-D 维持 WAITING_FOR_CASE2_TICKET。详见 [final-night-report](../evidence/rpd-24h/night-run/final-night-report.md)、
[D-A wiring-report](../evidence/rpd-24h/D-A/wiring-report.md)。

## 最终状态（前轮）：MANUAL-REQUIRED

人工决策点：①CASE2 门决策 ②D-1/D-2 真实审批 ③controller/Worker 正式注入 ④真实 CASE2 执行 ⑤凭据通道统一。
PR：https://github.com/nghqqa/MergePilot/pull/233

## 已知事实 → 待证据复核映射（全部已复核）

| 已知事实 | 复核任务 | 命令 |
|---|---|---|
| pgvector smoke 11/11 | RPD-02 | 复核脚本+commit 记录 |
| TicketStore 13/13 + 26 单测 | RPD-04 | gate_ticket_smoke.py + pytest |
| 回归 281 / PG 25-26 | RPD-05 | pytest 四目录 + MERGEPILOT_PG_CONTRACT=1 |
| case_retrieval 接线 9/11→待重验 | RPD-03 | pytest test_case2_fixes + validate_env |
