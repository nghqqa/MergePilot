# AgentLoop 接入决策与配置清单（Phase 6 前置）

> 日期：2026-08-27　依据：控制台截图（接入中心，workspace `goai`，region 华东1·杭州）＋官方文档
> 纪律：本文档只允许出现占位符；真实 endpoint / LicenseKey / Project / WorkspaceId 只进环境变量，绝不入库。

## 一、接入路径裁定

接入中心 37 项接入中**没有** HiClaw/AgentTeams，也没有面向自研框架的 LoongSuite Pilot 原生支持 → 按预研结论走 **「自定义接入」= 标准 OTLP/HTTP**。与我们已冻结的"单出口 exporter"架构一致，**零架构改动**，仅配置接线。

官方协议约束（[OpenClaw 接入文档](https://help.aliyun.com/zh/document_detail/3042581.html)，同一接入中心体系）：

> `otel.protocol`：**当前仅支持 http/protobuf**；"仅支持 HTTP/Protobuf，暂不支持 HTTP/JSON 和 gRPC"；gRPC 设置会被静默忽略。

## 二、官方参数 → 本工程环境变量映射（全部官方确认名，无臆造）

| 官方参数/头 | 官方环境变量降级 | 本工程接线 |
|---|---|---|
| endpoint（完整 URL，示例形如 `https://proj-xtrace-xxx.cn-hangzhou-intranet.log.aliyuncs.com/apm/trace/opentelemetry/v1/traces`） | `ARMS_OTLP_ENDPOINT` | `MP_OTLP_ENDPOINT` |
| 头 `x-arms-license-key` | `ARMS_LICENSE_KEY` | `OTEL_EXPORTER_OTLP_HEADERS` 内 `x-arms-license-key=<值>` |
| 头 `x-arms-project` | `ARMS_PROJECT` | 同上 `x-arms-project=<值>` |
| 头 `x-cms-workspace` | `ARMS_CMS_WORKSPACE` | 同上 `x-cms-workspace=<值>` |
| serviceName | `ARMS_SERVICE_NAME` / `OTEL_SERVICE_NAME` | `OTEL_SERVICE_NAME=mergepilot`（代码已实现官方名优先） |
| 协议 | — | `MP_OTEL_EXPORT_FORMAT=proto`（本轮新增 OTLP protobuf 编码，JSON 保留给本地 otelcol） |
| 资源属性 | `OTEL_RESOURCE_ATTRIBUTES` | 已支持解析（k=v URL-decoded） |

## 三、你需要做的 4 步（每步 1 分钟）

1. 接入中心 → 点开**「自定义接入」**卡片（2 项中与 OpenTelemetry/OTLP 相关的那个）；
2. 复制 4 个值：**完整 OTLP endpoint（含 /v1/traces）、LicenseKey、Project、WorkspaceID**；
3. 在**你的终端会话**里设置以下变量（占位符替换成第 2 步的值；不要写进任何文件入库）：

   ```bash
   export MP_OTEL_EXPORT_ENABLED=1
   export MP_OTEL_EXPORT_FORMAT=proto
   export MP_OTLP_ENDPOINT='<endpoint 完整 URL>'
   export OTEL_SERVICE_NAME=mergepilot
   export OTEL_EXPORTER_OTLP_HEADERS='x-arms-license-key=<LicenseKey>,x-arms-project=<Project>,x-cms-workspace=<WorkspaceId>'
   ```

4. 回我一句"已配置"，我立即执行 Phase 6 最小连通性验证（`poc_health_check` 发 Entry+Tool 两段 span → 控制台核对 Trace ID / service.name / 父子关系 / 无重复上报 / 无泄漏）。

## 四、工程就绪度（本轮新增）

- OTLP **protobuf** 编码器（无第三方依赖，手写 wire format，含 links/status/kind/resource attrs），37 项离线测试含结构级回读断言；
- 认证头通道（`OTEL_EXPORTER_OTLP_HEADERS`，官方标准名）+ `OTEL_SERVICE_NAME` 官方优先级 + `OTEL_RESOURCE_ATTRIBUTES` 解析；
- 顺带修复：json 分支缺 `content_type` 赋值导致导出静默失败的实现 bug（此前被 fail-open 吞掉，测试已补锁定）。

## 五、配置状态总表（v0.3）

| 项 | 状态 | 依据 |
|---|---|---|
| AgentLoop 开通 | READY | 控制台已可见（接入中心/费用入口） |
| region | READY | 华东1（杭州）· 控制台 |
| workspace/AgentSpace | READY | `goai` · 控制台 |
| 认证头格式 | READY | 官方文档三头 + ARMS_* 降级名 |
| service.name 规则 | READY | `OTEL_SERVICE_NAME` 官方环境变量 |
| OTLP endpoint 具体值 | **NEEDS_USER_ACTION** | 需从自定义接入卡片复制 |
| LicenseKey/Project/WorkspaceID 具体值 | **NEEDS_USER_ACTION** | 同上 |
| 本机 env 接线 | MISSING | 待第 3 步 |
| 云侧连通性 | BLOCKED_CONFIGURATION | 待上行两项完成后重判 |
