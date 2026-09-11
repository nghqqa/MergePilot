# AgentTeams + CoPaw 复赛演示环境 —— 最终架构说明（方案 A 定稿）

> 密级：提交包（脱敏）。本文件不含任何 password、token、API key、Cookie。
> 方案：**A — 保留 p14h2-wd OpenClaw 主线 + p14h2-copaw CoPaw 演示 runtime**。

## 一、项目目标

在 Windows Docker Desktop 单一 Linux Engine 上，以 AgentTeams v1.2.3（commit 223ddc2）为协作框架，
构建并验证「Leader 自主派发 → 多 Agent 协作执行 → 人工审批门」的完整闭环：

```
kickoff → projectflow(ready_nodes) → taskflow(delegate_task)
→ Reviewer 执行 → Fixer 执行 → Verifier 执行 → 最终报告 → HUMAN_APPROVAL_REQUIRED
```

## 二、系统架构

```
Windows Docker Desktop (desktop-linux context, engine 29.7.2, 数据盘 D:)
│
├── p14h2-wd-net (172.31.40.0/24, 主线)
│     └── p14h2-wd-ctrl  agentteams-embedded:223ddc2
│           ├─ Controller API :8090   ├─ MinIO :9000   ├─ Matrix(Tuwunel) :6167
│           ├─ Higress AI Gateway :8080 → DeepSeek provider
│           └─ Higress Console :8001 (AI provider / consumers 管理)
│     ├── agentteams-worker-p14h2-wd-worker-{manager,reviewer,fixer,verifier}
│     │     runtime=openclaw  image=agentteams/worker-agent:223ddc2
│     └── project wd1-pr1-bootstrap (主线保留，active)
│
└── p14h2-copaw-net (演示 runtime；控制器双挂提供 Control/Matrix/MinIO/LLM)
      ├── agentteams-worker-p14h2-copaw-worker-{manager,reviewer,fixer,verifier}
      │     runtime=copaw  image=agentteams/copaw-worker:223ddc2-build1
      │     （原生 projectflow/taskflow/delegate_task/message/filesync 工具）
      └── team p14h2-copaw + project copaw-sandbox (completed, 全 [x])
```

## 三、CoPaw 选择原因

- AgentTeams 的**项目/DAG 委派工具（projectflow/taskflow/delegate_task）是 CoPaw 运行时原生
  hook 工具**（copaw/src/copaw_worker/hooks/tools/），openclaw 运行时不含；
- CoPaw 源码树（D:\goai\p12\agentteams-v123，git HEAD=223ddc2）含完整 Dockerfile + 15 个测试文件，
  可构建可追溯（构建产物 sha256:cdc8f8a4ab8d66…）；
- credential_guard / output_sanitizer 安全钩子实测生效。

## 四、部署方式（Windows Docker Desktop）

- Engine：desktop-linux context → dockerDesktopLinuxEngine 命名管道；程序与数据盘位于 D 盘；
- 镜像：三个 223ddc2 镜像经本地验证 tar 导入；copaw-worker 镜像由 copaw/Dockerfile 本地构建
  （Aliyun 镜像源参数，控制器阶段钉定本地 223ddc2 镜像）；
- DNS：Docker 引擎 DNS 曾被宿主 TUN 代理 fake-IP 劫持——通过 mihomo fake-ip-filter 排除
  `+.deepseek.com/+.dnse1.com` 修复（官方 reload API，204）；
- LLM：worker → Higress AI Gateway(:8080, key-auth consumer) → deepseek provider → api.deepseek.com。

## 五、E2E 时间线（copaw-sandbox，全部 Agent 自主）

1. kickoff 送达 copaw Leader DM（event $i5h9A0xjA5NGQhNQv7NScmfL3_hg_sLLRx8Tjb0CemY）
2. Leader 消费 → agent run（session 落盘）
3. projectflow(ready_nodes) → [review-1]（首次因 plan store 未播种阻塞，播种后重试成功）
4. taskflow(delegate_task) → review-1 派发 reviewer（Team Room @mention）
5. reviewer ack_task → 执行 review → result.md（SUCCESS）→ 回报 manager 房间
6. e2e-continuation 触发 → Leader 读结果 → 派发 fix-1 → fixer 执行（SUCCESS）
7. Leader 派发 verify-1 → verifier 执行（SUCCESS：全链端到端验证通过）
8. Leader 产出最终报告 → 项目状态 completed → **HUMAN_APPROVAL_REQUIRED**

## 六、三任务结果

| 任务 | 执行者 | 结果 | 产物 |
|---|---|---|---|
| review-1 | p14h2-copaw-worker-reviewer | SUCCESS | review-findings.md |
| fix-1 | p14h2-copaw-worker-fixer | SUCCESS（无需修复） | fix-findings.md |
| verify-1 | p14h2-copaw-worker-verifier | SUCCESS（全链验证通过） | verify-findings.md |

## 七、自主修复案例

Leader 自主修复 plan.md 双段落缺陷（播种残留 + 运行时重写导致重复段阻塞 ready_nodes/plan_dag）：
去重段落、重置过早的 delegated 标记，随后 DAG 正常推进。1 次修复，限额内，有 session 证据。

## 八、稳定性

- 6/6 容器全程 running；RestartCount=0；StartedAt 恒定（无 reconcile 删除/重建）；
- 180s 稳定窗多轮通过（WD1-F、E2E-FULL、本阶段快照）。

## 九、已知限制（如实声明）

1. 控制器 project store 与 CoPaw 文件存储为**双存储**（API 建项目需操作员播种文件 store；
   或改用 Leader 原生 create_project 流程）；
2. 上游 223ddc2 测试漂移/污染（87 失败已定性为陈旧测试+测试间污染，非镜像缺陷）；
3. copaw lite/headless 模式存在 worker_port 边界 bug（标准模式+console-port 已规避）；
4. LLM key 与 consumer key 曾回显进会话记录（轮换建议已给出，见凭据风险审计）。

## 十、当前 PR 状态

nghqqa/fastapi-boilerplate-demo#1 **保持 open**。本 runtime 从未执行任何
merge/close/reopen/comment/push 操作。

## 十一、人工审批要求

见 human-approval-record.json / human-gate-report（E2E-FULL 目录）：需人工批准沙箱 E2E 结果、
裁决主线迁移方向、授权 PR 操作与凭据轮换。

## 十二、边界声明

- **AgentTeams** = 多 Agent 协作框架（Controller/Team/Worker/Project）；
- **CoPaw** = Agent 运行时（内含委派工具的 python runtime）；
- **p14h2-copaw** = 本机隔离演示 runtime；
- **p14h2-wd** = OpenClaw 主线（本阶段保持不变）。
- 主线未迁移到 CoPaw；三任务由 CoPaw runtime 执行；OpenClaw worker 的 52 次工具调用属于
  p14h2-wd runtime 的独立验证记录。
