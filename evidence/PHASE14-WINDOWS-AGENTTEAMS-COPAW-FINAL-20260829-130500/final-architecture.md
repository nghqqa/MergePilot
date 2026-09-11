# AgentTeams + CoPaw 复赛演示环境 —— 最终架构说明

> Phase 14.2H 全系列最终提交包（脱敏）。本文件不含任何 password、token、API key、Cookie。
> 裁决：`PHASE14_2H_WINDOWS_AGENTTEAMS_COPAW_SUBMISSION_READY`

## 一、目标与结论

在 Windows Docker Desktop 单一 Linux Engine 上，以 **AgentTeams v1.2.3**（commit 223ddc2）为协作框架，
验证了 **CoPaw Leader 全自主项目闭环**：

```
kickoff → projectflow(ready_nodes) → taskflow(delegate_task)
→ Reviewer 执行 → Fixer 执行 → Verifier 执行 → 最终报告 → HUMAN_APPROVAL_REQUIRED
```

最终裁决：`COPAW_AGENT_AUTONOMOUS_PROJECT_CLOSED_PENDING_HUMAN_GATE`（人工门已批准）。

## 二、系统架构

```
Windows Docker Desktop（desktop-linux context，engine 29.7.2，程序与数据盘在 D:）
│
├── p14h2-wd-net (172.31.40.0/24, OpenClaw 主线 — 保留不变)
│     └── p14h2-wd-ctrl  agentteams-embedded:223ddc2
│           ├─ Controller API :8090      ├─ MinIO :9000
│           ├─ Matrix(Tuwunel) :6167     ├─ Higress AI Gateway :8080 → DeepSeek
│           └─ Higress Console :8001 (AI provider / consumers)
│     ├── agentteams-worker-p14h2-wd-worker-{manager,reviewer,fixer,verifier}
│     │     runtime=openclaw  image=agentteams/worker-agent:223ddc2
│     ├── agentteams-manager (主线 Manager，保留)
│     └── project wd1-pr1-bootstrap (active，主线保留)
│
└── p14h2-copaw-net (CoPaw 演示 runtime — 本提交包主体)
      └── agentteams-worker-p14h2-copaw-worker-{manager,reviewer,fixer,verifier}
            runtime=copaw  image=agentteams/copaw-worker:223ddc2-build1
            （原生 projectflow/taskflow/delegate_task/message/filesync 工具
              + credential_guard / output_sanitizer 安全钩子）
      ├── team p14h2-copaw (Active)
      └── project copaw-sandbox (completed，全 [x])
```

- 控制器双挂到 p14h2-copaw-net（additive、可逆），为沙箱提供 Control/Matrix/MinIO/LLM；
- 零 WSL 参与；两套 runtime 的 worker 各自只挂自己的网络。

## 三、CoPaw 选择原因

AgentTeams 的项目/DAG 委派工具（projectflow/taskflow/delegate_task）是 **CoPaw 运行时原生
hook 工具**（copaw/src/copaw_worker/hooks/tools/）；openclaw 运行时不含。CoPaw 源码树
（git HEAD=223ddc2，与镜像构建 commit 一致）含完整 Dockerfile 与 15 个测试文件，可构建可追溯。

## 四、部署方式（Windows Docker Desktop）

- Docker Desktop：desktop-linux context → dockerDesktopLinuxEngine 命名管道；程序与数据盘在 D 盘；
- 镜像：三个 223ddc2 镜像经本地验证 tar 导入（digest 与源码 commit 对应）；copaw-worker 镜像本地构建；
- DNS：宿主 TUN 代理 fake-IP 劫持经 mihomo fake-ip-filter 排除 `+.deepseek.com/+.dnse1.com` 修复
  （官方 reload API，204）；
- LLM：worker → Higress AI Gateway(:8080, key-auth consumer) → deepseek provider → api.deepseek.com。

## 五、E2E 时间线（copaw-sandbox，全部 Agent 自主）

1. kickoff 送达 copaw Leader DM（event $i5h9A0xjA5NGQhNQv7NScmfL3_hg_sLLRx8Tjb0CemY）→ Leader 消费
2. projectflow(ready_nodes) → [review-1]
3. taskflow(delegate_task) → review-1 派发 reviewer（Team Room @mention）
4. reviewer ack_task → 执行 review → result.md（SUCCESS）→ 回报 manager 房间
5. e2e-continuation → Leader 读结果 → 派发 fix-1 → fixer 执行（SUCCESS）
6. Leader 派发 verify-1 → verifier 执行（SUCCESS：全链端到端验证通过）
7. Leader 产出最终报告 → 项目状态 completed → **HUMAN_APPROVAL_REQUIRED**

（期间 Leader 还自主修复了 plan.md 双段落缺陷——去重 + 标记重置，1 次限额内。）

## 六、三任务结果

| 任务 | 执行者 | 结果 | 产物 |
|---|---|---|---|
| review-1 | p14h2-copaw-worker-reviewer | SUCCESS | review-findings.md |
| fix-1 | p14h2-copaw-worker-fixer | SUCCESS | fix-findings.md |
| verify-1 | p14h2-copaw-worker-verifier | SUCCESS | verify-findings.md |

## 七、稳定性

- 6/10 容器多轮 180s 稳定窗 PASS（StartedAt 恒定、RestartCount=0、无 reconcile 删除/重建）；
- 凭据轮换（consumer 5/5 + DeepSeek provider）后 180s 窗 10/10 running、认证错误 0。

## 八、已知限制（如实声明）

1. 控制器 project store 与 CoPaw 文件 store 为**双存储**（API 建项目需操作员播种文件 store；
   或改用 Leader 原生 create_project 流程）；
2. 上游 223ddc2 测试漂移/污染（87 失败已定性为陈旧测试+测试间污染，非镜像缺陷）；
3. copaw lite/headless 模式 worker_port 边界 bug（标准模式+console-port 已规避）；
4. LLM key 与 consumer key 曾回显进会话记录（轮换已完成，旧 key 已失效）。

## 九、当前 PR 状态

nghqqa/fastapi-boilerplate-demo#1 **保持 open**。本 runtime 从未执行任何
merge/close/reopen/comment/push 操作。

## 十、人工审批

HUMAN_APPROVAL_REQUIRED → **已批准**（2026-08-29T13:25+08:00，操作员；见
human-approval-final.json / PROMOTION-A-FINAL human-approval-record.json）。

## 十一、边界声明

- **AgentTeams** = 多 Agent 协作框架（Controller/Team/Worker/Project/DAG）；
- **CoPaw** = Agent 运行时（原生委派工具的 python runtime）；
- **p14h2-copaw** = 本机隔离演示 runtime；
- **p14h2-wd** = OpenClaw 主线（保持不变）。
- 主线未迁移到 CoPaw；三任务由 CoPaw runtime 执行。
