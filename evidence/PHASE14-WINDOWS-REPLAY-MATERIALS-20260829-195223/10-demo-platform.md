# 演示平台页面设计与状态字段设计

> 目标：把复赛双案例做成评委可自助浏览的单页演示平台（只读为主，人工门为唯一写操作入口）。
> 数据源全部来自真实系统：Controller API、Matrix 房间事件、任务存储（shared/）、审批记录。

## 一、页面结构（单页三栏）

```
┌────────────────────────────────────────────────────────────────────┐
│ 顶栏: AgentTeams Demo | 案例切换 [PR#1 普通协同] [PR#2 高危安全门]      │
│       全局状态徽标: PR#1 COMPLETED | PR#2 VERIFIED·PR OPEN           │
├──────────────┬─────────────────────────────────┬───────────────────┤
│ 左栏: 团队     │ 中栏: 主视图（随案例切换）            │ 右栏: 审计与边界     │
│  Agent 名册   │  PR#1: DAG 自主闭环时间线            │  人工门面板          │
│  在线/角色    │  PR#2: 门节点高亮的 DAG + 探针对照     │  诚实披露清单        │
│  心跳/状态    │  Matrix 房间事件流（按 event_id 定位） │  范围边界(未实现项)   │
│              │  任务卡(状态机着色)                   │  SHA256 完整性面板   │
└──────────────┴─────────────────────────────────┴───────────────────┘
```

### 区块明细

1. **Agent 名册**（左）：角色/Matrix ID/运行时镜像/最近心跳；点击进入该 worker 的控制台外链。
2. **DAG 视图**（中）：按 04-dag.md 渲染；节点色 = 任务状态机着色
   （灰 pending / 蓝 assigned / 黄 in_progress / 绿 submitted / 深绿 completed / 红 failed）；
   PR #2 的门节点闪烁显示 `GATE: APPROVED`。
3. **事件流**（中下）：Matrix 房间时间线，每条含 event_id（可复制）、sender、m.mentions、
   正文摘要；提供 `$IDofCrGE… / $JU09kIgw… / $t0dXkuWw…` 一键定位。
4. **任务卡**（中）：任务目录树 + meta/result 摘要 + `check_task` 实时按钮
   （展示 ok/status/resultStatus/effective 原始 JSON）。
5. **人工门面板**（右）：门状态机、批准记录渲染（markdown）、操作者签名时间；
   未批准状态下显示"受控停等"并有禁用态的派发按钮（只读演示中该按钮永不可点）。
6. **诚实披露面板**（右）：07 的三项事件 + 08 的未实现清单，默认展开（主动披露）。
7. **完整性面板**（右）：SHA256SUMS 实时校验状态（PASS/DRIFT）。

## 二、状态字段设计

### 2.1 顶层案例对象（GET /api/cases/{case_id}）

```json
{
  "case_id": "pr2-high-risk-human-gate",
  "title": "PR #2 — 高危安全门闭环",
  "pr": {
    "repo": "nghqqa/fastapi-boilerplate-demo",
    "number": 2,
    "branch": "demo/high-risk-human-gate",
    "state": "OPEN",
    "merge_policy": "FORBIDDEN_IN_DEMO"
  },
  "phase": "verified",
  "agents": [ /* 见 2.2 */ ],
  "tasks": [ /* 见 2.3 */ ],
  "human_gate": { /* 见 2.4 */ },
  "timeline_ref": "05-timeline.md",
  "integrity": { "sha256sums_verified": true }
}
```

### 2.2 Agent 对象

| 字段 | 类型 | 来源 | 示例 |
|---|---|---|---|
| agent_id | string | Matrix user_id | `p14h2-copaw-worker-reviewer` |
| matrix_id | string | 同上 | `@p14h2-copaw-worker-reviewer:…:6167` |
| role | enum | 配置 | reviewer / fixer / verifier / leader / human |
| runtime | string | 镜像 tag | `copaw-worker:223ddc2-build2` |
| state | enum | controller API | online / idle / working / offline |
| current_task | string? | 任务 meta | `review-1` |

### 2.3 任务对象（核心状态字段）

| 字段 | 类型 | 枚举/说明 | 来源 |
|---|---|---|---|
| task_id | string | 如 `review-1` | plan/meta |
| project_id | string | 项目隔离键 | meta |
| assignee | string | worker MXID | meta.assigned_to |
| **status** | enum | `pending → assigned → in_progress → submitted → completed` / `failed` | meta.status（存储权威） |
| notification | object | `sent: bool`、`event_id`、`event_id_stale: bool`、`reused: bool` | meta.event_id + 房间核对 |
| result_status | enum? | `SUCCESS / INTERRUPTED / VERIFICATION_PASSED*`（*协议扩展待入白名单） | result.md |
| effective | bool | Leader 验收 | check_task |
| evidence | string[] | 工件相对路径 | workspace/ |
| gate_dependency | enum? | `none / human_gate_required / approved / rejected` | 人工门服务 |

**状态机**（与 04-dag.md 一致）：

```
pending ─delegate(发送成功+event_id)→ assigned ─ack→ in_progress ─submit→ submitted ─验收→ completed
                                                                                   └→ failed
委派失败: pending(重试) ；通知丢失自动检测: notification.event_id_stale=true
```

### 2.4 人工门对象

```json
{
  "gate_id": "copaw-high-risk-human-gate:post-review",
  "trigger": {
    "by": "review-1",
    "signals": ["FINDING_CONFIRMED", "SEVERITY: HIGH", "HUMAN_VERIFICATION_REQUIRED: YES"]
  },
  "state": "approved",              // pending | approved | rejected | timeout
  "approved_at": "2026-08-29T10:5x:xxZ",
  "record_path": "shared/projects/copaw-high-risk-human-gate/human-gate-approval.md",
  "scope": ["dispatch fix-1", "minimal fix + local tests", "dispatch verify-1"],
  "prohibitions": ["merge", "push", "close", "reopen"],
  "unblocks": ["fix-1", "verify-1"]
}
```

### 2.5 完整性对象

```json
{
  "sha256sums": { "path": "SHA256SUMS", "verified": true },
  "disclosures_open": ["minio-shared-tree-emptied(restore)","tool-guard-timeout(mitigated)","status-enum-gap(documented)"],
  "not_implemented": ["polardb-rag", "agentic-db-branch", "agentloop-otel"]
}
```

## 三、数据接入映射（平台 → 真实系统）

| 平台字段 | 真实来源 |
|---|---|
| agents.state | Controller API `GET /api/v1/workers`（Bearer） |
| tasks.status / result_status / effective | `taskflow check_task`（shared/ 存储 + validate） |
| 事件流 | Matrix `GET /rooms/{team}/messages`（dir=b 分页，只读） |
| human_gate | 审批记录文件 + 审批服务状态 |
| integrity | 平台对材料目录实时重算 SHA256 |

## 四、实现建议

- 静态优先：单页（任意框架）+ 只读代理 API；唯一写操作（人工门批准）走审批服务并落盘。
- 全部只读代理需 Bearer 注入在服务端，页面不持有任何凭据（呼应无 Secret 约束）。
- 状态刷新：任务/agent 轮询 30s；事件流手动"定位 event_id"触发即可（避免演示时抖动）。
