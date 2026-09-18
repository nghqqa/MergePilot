# MergePilot 演示平台部署指南

## 前提条件

- Node.js ≥ 18（后端零第三方依赖，前端已预构建）
- 现代浏览器（Chrome / Edge / Firefox）
- 无需数据库、无需 Docker、无需网络（完全离线可运行）

## 一键启动

### Windows

双击 `start-demo.bat`，或命令行：

```bat
cd MergePilot-demo
node backend/server.mjs
```

### macOS / Linux

```bash
cd MergePilot-demo
node backend/server.mjs
```

启动后访问 http://127.0.0.1:4173

## 健康检查

```bash
curl http://127.0.0.1:4173/api/health
# 期望输出（关键字段）：
# agentloop.live_copaw_run.status = VERIFIED
# rag.status = SYNTHETIC DEMO
# polardb.connection = NOT CONNECTED
```

## 自测

```bash
cd backend
node test/selftest.mjs
# 期望输出：selftest: 54 passed, 0 failed
```

## 页面清单

| 路径 | 页面 | 说明 |
| --- | --- | --- |
| `/` | Overview | 状态栏 + 三条安全路径 + AgentLoop 权威数据 + 案例入口 |
| `/cases/pr1-normal-review` | PR #1 自主完成 | Reviewer → Fixer → Verifier 全绿 |
| `/cases/pr2-high-risk-human-gate` | PR #2 人工批准 | CWE-22 · 闸门 APPROVED · 探针 200→404 |
| `/cases/pr3-high-risk-human-reject` | PR #3 人工拒绝 | CWE-78 · 409 终态 · 永久 BLOCKED |
| `/pr4` | PR #4 跨仓 Schema | RAG 命中 → 三候选 → 隔离 Branch 验证 → 人工门 |
| `/rag` | RAG 检索 | 8 篇合成文档 · 9 分块 · 检索演示 |
| `/ops` | Operations | RAG / PolarDB 状态 · 最近查询 · AgentLoop 关联 |
| `/audit` | 审计 | 组件状态 · 完整性 · 边界披露 |

## 现场演示推荐动线

1. **Overview**：状态栏全景 → 三案例入口（30 秒）
2. **PR #2**：翻到 PRESENTATION 视图，播放闸门批准事件（1 分钟）
3. **PR #3**：翻到拒绝路径 + 409 终态（1 分钟）
4. **PR #4 页**：RAG 命中 → 三候选对比 → Branch 验证 → 人工门（2 分钟）
5. **Operations**：RAG 状态 → PolarDB 门槛 → AgentLoop 权威数据（1 分钟）
6. **Trace 截图**（如控制台可打开）：`fbf4a3...` 链路图（1 分钟）

## 故障回退

| 风险 | 回退 |
| --- | --- |
| 平台页面异常 | 刷新；仍异常则播放演示视频 |
| 网络不可用 | 平台完全离线可运行，不受影响 |
| 云端控制台不可达 | 展示操作员确认截图（已归档） |
| 被追问未接入的模块 | 如实说边界 + 已列入路线图，不编造 |
