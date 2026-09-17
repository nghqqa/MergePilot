# v3 埋点（四问题修复）— 暂存状态与恢复步骤

> 2026-09-17：v3 代码已完成并暂存于 `image/zz_agentloop_otel_v3.py`。
> 会话 shell 故障（bash.exe ENOENT）导致构建/验证未执行——shell 恢复后按以下步骤继续。

## 已实现（对照四个问题）

1. **LLM 双层冗余**：`_apply_agentscope_model` 检测到 retry_chat_model 已在路径时跳过内层
   patch → 每次 LLM 调用只产生一个 `genai.llm.call` span。
2. **genai 语义**：工具 span 经 loongsuite `ExecuteToolInvocation` handler（可用时）→
   控制台"工具调用"计数点亮；所有 span 携带 `gen_ai.conversation.id`；handler 缺失时回退原始 span。
3. **跨 Agent 关联（FIX-3）**：
   - 发送侧 `_send_matrix_room_message` 注入当前 traceparent 到事件内容键
     `m.agentloop.traceparent`（body 不动）；
   - 接收侧 `MatrixChannel._was_mentioned` 解析并发射 `agentteams.delegation.link` span
     （parent=发送方 span）→ 委派出现在 Leader 瀑布图中。
4. **线性说明**：已答复（ReAct 串行真实形状）；跨 Agent 关联落地后瀑布将呈现委派子节点。

## 恢复步骤（shell 恢复后依序执行）

```bash
export MSYS_NO_PATHCONV=1
cd /d/goai/r3work/image
# 1) 语法检查 + 单层 LLM 断言（临时容器，假端点）
python -m py_compile zz_agentloop_otel_v3.py
docker run --rm --entrypoint /opt/venv/standard/bin/python \
  -e AGENTLOOP_OTEL_CONFIG=/tmp/cfg.json -e AGENTLOOP_OTEL_GRACE=1 \
  -e AGENTTEAMS_WORKER_NAME=mech -v "D:/goai/r3work/image:/mnt/zz:ro" \
  agentteams/copaw-worker:223ddc2-agentloop /mnt/zz/../image/_v3test.py
# （_v3test.py 待写：启用 cfg → 导入 retry_chat_model 与 agentscope.model →
#   断言仅 RetryChatModel 被 patch（单层）；traceparent 注入 _send_matrix_room_message 单测）

# 2) 构建 v3 镜像（新 Dockerfile：在 -agentloop 之上 COPY zz_agentloop_otel_v3.py 为 zz_agentloop_otel.py）
#    tag 建议 agentteams/copaw-worker:223ddc2-agentloop-v3

# 3) 金丝雀验证（4184 RAG 服务需在宿主运行）：
#    agt create worker --name canary3 --image ...-v3 → 双开关 → @mention：
#    a) 回复正常（dispatch 不破） b) 每个 LLM 调用只有一层 span
#    c) 消息工具发送的事件导出含 m.agentloop.traceparent 键

# 4) （可选）真实一轮委派验证 delegation.link span 出现在 Leader 瀑布
```

## 注意

- v3 只用于**未来运行**；已交付的 RAG-TRACED 证据包由 v2.2 产出，勿混淆版本声明。
- `_was_mentioned_attrs` 中的会话 ID 目前取 worker 级（room/task 哈希）；如需按会话精确分组，
  需从事件 room_id 提取（nio event 有 room_id 字段时可增强）。
- loongsuite handler 若镜像缺失，GENAI_HANDLER_ABSENT 会写进 audit log（回退 raw span，不影响运行）。
