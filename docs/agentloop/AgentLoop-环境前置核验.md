# AgentLoop 环境前置核验（PoC Phase 1）

> 日期：2026-08-27  
> 分支：`feat/agentloop-otel-genai-poc`  
> 方法：只探测配置**是否存在**，不读取、不打印任何值、长度、哈希或前后缀；不搜索本机其他凭证来源。
> 本文档无任何真实凭证内容。

## 核验方法与证据

1. 从代码提取本工程 OTel 出口约定：`tools/otel/` 引用的变量族为 `SLS_ENDPOINT / SLS_PROJECT / SLS_LOGSTORE / SLS_ACCESS_KEY_ID / SLS_ACCESS_KEY_SECRET` 及 batch/retry 调参项，另有运行时令牌 `COORDINATOR_TOKEN / GATEWAY_TOKEN`（非观测用途）。**当前代码没有任何 AgentLoop 专用接入点或变量名**——即现有出口是"本地 OTLP receiver + 规划中的 SLS exporter"，AgentLoop 出口尚不存在。
2. 会话环境变量按名匹配 `AGENTLOOP / AGENTSPACE / OTEL / SLS / MSE / CMS / ALIBABA / ALIYUN / LICENSE / WORKSPACE / ENDPOINT / TELEMETRY`：仅命中与本工程无关的 `HF_ENDPOINT`。**未发现任何观测出口凭证或 endpoint**。
3. 仓库内无 `.env*` 文件，无 `*agentloop*` 命名的实现文件；提及 agentloop 的仅有 docs 规划文本。

## 逐项结论

| 检查项 | 状态 | 说明 |
|---|---|---|
| AgentLoop 已开通 | NEEDS_USER_ACTION | 用户声明云侧已开通；本机无法证伪/证实。需提供可连通性证据之一（endpoint 配置 + 认证变量），Phase 6 才能实连 |
| AgentSpace 已创建 | NOT_VERIFIED | 云侧控制台信息，本地不可探测 |
| region | MISSING | 本地无对应配置键 |
| workspace | MISSING | 本地无对应配置键 |
| OTLP endpoint | MISSING | 无标准 `OTEL_EXPORTER_OTLP_*` 变量，也无 AgentLoop 专用 exporter 实现 |
| License / 认证环境变量 | MISSING | 无相关变量名存在 |
| CMS 2.0 | NOT_VERIFIED | 云侧依赖，需控制台确认开通与否 |
| SLS | NOT_VERIFIED | 代码有 SLS exporter 与 SLS_* 变量约定，但环境未配置任何 SLS_* 键（视为未接通） |
| MSE AI 治理中心 | NOT_VERIFIED | 云侧依赖，需控制台确认 |
| RAM 权限 | NEEDS_USER_ACTION | 需用户提供最小权限说明（仅 Trace 上传相关 action），不得在本仓库存放 AK |
| Trace 保存周期 | NOT_VERIFIED | 平台侧参数，待答疑确认 |
| 数据采样设置 | NOT_VERIFIED | 平台侧参数；PoC 期建议 parentbased_traceidratio=1.0（全采样）以支撑门禁统计 |

## 关键配置缺失时的执行纪律（本轮生效）

- 判定：**关键配置（OTLP endpoint + 认证）MISSING → 连通性与真实链路阶段（Phase 6/7 实连部分）暂不可执行**；
- 继续完成零凭证设计与离线实现/测试（Phase 3–5、8–9 的离线部分），不猜测 endpoint，不搜寻其他凭证；
- 若会话结束前用户补齐 endpoint+认证并经最小健康检查通过，才升级为 READY 进入 Phase 6。

**当前汇总状态：BLOCKED_CONFIGURATION（仅阻塞联网验证环节；离线工作照常推进）**
