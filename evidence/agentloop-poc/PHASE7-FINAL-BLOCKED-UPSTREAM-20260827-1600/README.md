# Phase 7 最终收口 — BLOCKED_UPSTREAM_TUWUNEL_ROUTE

## 分类账（README 之三，权威版）
- **VERIFIED**：Phase 6 AgentLoop 最小 OTLP（protobuf+认证头）+ 服务端接受 + 控制台原始 Trace（logstore 4 span 逐字段对账）；Phase 7 worker/transport/GenAI 防伪造接线的全部离线门禁（121+52+46）；snapshot_worker 密码供给、gw 预热重试、WSL Matrix transport 三个产品级修复（带测试）。
- **NOT_VERIFIED**：真实四 Agent 链路（Manager→Reviewer→Fixer→Verifier 从未执行）；AgentLoop AI Agent 语义页面；LLM/tool/token/retrieval/memory span（零伪造）。
- **唯一阻塞**：外部 hiclaw-embedded:v1.1.2 内嵌 tuwunel 1.5.0-48 在**容器重建后新房间路由失效**（决定性复现：createRoom=200 而 state/invite 确定性 404 M_UNRECOGNIZED，跨双卷/多实例/三视角）。
- **模型调用：0**。
- **不构成生产可用性结论。**

## 恢复路径
1. 升级/更换 hiclaw-embedded 内嵌 tuwunel 构建 → 新卷重跑（供给脚本幂等全备）。
2. 受控环境采用「首次建全资源、后续仅 docker restart」生命周期规避——**实验已证可行但非通用修复**。
