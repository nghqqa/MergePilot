# agentloop-copaw-image — CoPaw worker 镜像 + AgentLoop OTel 埋点（v2.2 延迟 patching）

> 产出镜像：`agentteams/copaw-worker:223ddc2-agentloop`（image ID `8a6b8995ccc2`，同 ID 另有运行 tag `-agentloop-v22`）。
> 基于 `agentteams/copaw-worker:223ddc2-build1`，仅叠加 3 个文件；开关默认关闭（`enabled:false`），对运行时行为零影响。

## 为什么是 v2（两代教训，A/B 实证）

1. **不能在 `.pth` 期 import copaw 模块**：v1 在解释器启动时 `__import__` 全部目标模块并打补丁，导致 copaw
   matrix bridge 入站派发完全失效（A/B：无 v1 时 @mention 秒级派发，有 v1 后零派发）。
   v2 的 `.pth` 导入**零第三方依赖、零副作用**，只启动一个 daemon watcher 线程。
2. **工具函数引用在注册时被捕获**：`copaw_worker/hooks/__init__.py` 把 `taskflow`/`projectflow`/`message`
   函数对象注册进 Toolkit，事后 patch 模块属性无效；nio 回调（`add_event_callback(self._on_room_event,…)`）
   同理绑定捕获。v2 只 patch **运行时按名查找**的稠点：

   | 稠点 | span | 说明 |
   |---|---|---|
   | `agentscope.tool._toolkit.Toolkit.call_tool_function` | `tool.<name>` | 覆盖全部工具调用；**注意它是"返回 async generator 的协程"**（内部 `return _object_wrapper(...)`），包装器必须保持协程形状（v2.1 曾误用 async-gen 包装 → `await` 处 TypeError，v2.2 修复） |
   | `matrix_channel.MatrixChannel._was_mentioned` | `matrix.receive` | 模块是 custom-channel 插件副本（`import_module("matrix_channel")` 顶层名），非 `copaw_worker.matrix_channel`；另有动态扫描兜底任何 `*matrix_channel` 模块 |
   | `matrix_channel.MatrixChannel.send` | `matrix.send` | agent 回帖 |
   | `copaw_worker.hooks.tools.message._send_matrix_room_message` | `matrix.send` | 工具侧发送（模块内全局名查找，事后 patch 有效） |
   | `copaw_worker.hooks.tools.taskflow._notify_task_assignment` | `taskflow.notify_assignment` | 捕获委派 Matrix eventId（结果属性） |
   | `copaw.providers.retry_chat_model.RetryChatModel.__call__` | `genai.llm.call` | 外层（含流式收口 + usage tokens） |
   | `agentscope.model.{OpenAI,DashScope}ChatModel.__call__` | `genai.llm.request` | 内层请求 |
   | `copaw.agents.react_agent.CoPawAgent.reply` | `agent.session.run` | agent 轮次 |

3. **延迟激活**：watcher 每 2s 轮询 `/etc/agentloop-otel.json`；`enabled:true` 且有 `otlp` 时才构建
   TracerProvider（此时应用已在运行，三方导入安全），再等 **90s 宽限**（copaw 完成自举）后 patch **已在
   `sys.modules` 中的模块（绝不主动 import）**。开关可运行时改写生效（`docker exec` 改文件，~2s 生效，零重启）。

## 内容策略

无正文采集：span 只带 hash/长度/ID/状态（`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NONE`）。
所有包装器异常安全，不改变被包装调用的结果；导出带状态记录（`/tmp/agentloop-model-audit.log` 的
`OTEL_EXPORT SUCCESS/FAILURE n=<count>` 行 = 直连导出的容器侧证据）。

## 构建与使用

```bash
docker build -t agentteams/copaw-worker:223ddc2-agentloop .
# 运行期启用（key 经 stdin/env 注入，不落镜像）：
AGENTLOOP_LICENSE_KEY=<key> python tools/agentloop/enable_otel.py on <container>...
# 直连导出显式验证（容器内，打印 HTTP 状态与 trace_id）：
docker cp tools/agentloop/otel_direct_probe.py <container>:/tmp/ && \
docker exec <container> /opt/venv/standard/bin/python /tmp/otel_direct_probe.py
```

配置文件字段：`{enabled, otlp, headers:{x-arms-license-key,x-arms-project,x-cms-workspace}, workspace,
service_name, schedule_delay_ms}`；环境变量覆盖：`AGENTLOOP_OTEL_CONFIG`（配置路径）、
`AGENTLOOP_OTEL_GRACE`（宽限秒数）。

## 运行记录（2026-09-16 决赛现场）

- 4 worker 全部 `7 patches applied`、0 错误；两个正式运行（PR #2 R3-TRACED 20m29s、PR #3 R2-TRACED 2m35s）
  产出 span 全部直连导出成功（PR2 快照 524 span/0 失败；会话累计 701 span/0 失败）。
- 证据：`evidence/FINALS-ELEM-PR2-R3-TRACED/`（含金丝雀 A/B 与 v2.1→v2.2 事故链 AUDIT.md）、
  `evidence/FINALS-ELEM-PR3-R2-TRACED/`。
