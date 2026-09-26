# Architecture

## 组件关系

```
┌─────────────────────────────────────────────────┐
│                  Docker Network                  │
│                 (internal only)                  │
│                                                  │
│  ┌──────────┐  ┌───────┐  ┌────────┐  ┌──────┐ │
│  │ Console   │→│  PG   │  │ MinIO  │  │(A链) │ │
│  │ (Node.js) │  │16-alp.│  │        │  │OFF   │ │
│  │ :4730     │  │ :5432 │  │ :9000  │  │      │ │
│  └──────────┘  └───────┘  └────────┘  └──────┘ │
│       ↓ loopback only                            │
└─────────────────────────────────────────────────┘
```

## Console（Node.js 零框架后端 + antd5 React 前端）

- **后端**：`console/backend/server.mjs`（零第三方依赖除 pg）
- **前端**：`console/frontend/`（React 18 + antd 5 + @ant-design/plots）
- **会话**：服务端内存（HMAC 签名 sid cookie `mp_session`）
- **allowlist**：`CONSOLE_REPO_ALLOWLIST` env，服务端行级过滤

## 数据流

1. Receipts（skill 调用回执）→ PG `skill_receipt_outbox`
2. Gate 决策 → PG `skill_gate_audit`
3. 票据 → PG `approval.tickets`
4. Console 五面 API 从 PG 实时读取（POSTGRESQL_LIVE）
5. 阶段推导由后端权威完成（票据/gate/回执完整性/head 排序）

## A 链（已关闭）

worker rag_retrieve → lexical-zh-en-v1 → 隔离 rag-live → 组织安全标准语料 → 审计。
Feature flag `MERGEPILOT_ORG_RAG_A_CHAIN` 控制，默认关闭。

## Fixer/Verifier（隔离就绪，未启动生产）

- Fixer：生成统一 diff（隔离 clone/fixture 模式）
- Verifier：独立验证（无 fixer_reasoning、测试结果必须来自 harness）
- 联调：CAS fencing + head 新鲜度 + patch digest 绑定
