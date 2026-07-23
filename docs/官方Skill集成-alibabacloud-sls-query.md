# 阿里云官方 Skill 集成：alibabacloud-sls-query

## 集成目的

Verifier 在测试和安全重扫完成后，按 `trace_id` 查询阿里云日志服务 SLS 中的 CI、Agent 与工具日志，确认修复后没有新增错误，并将查询结果作为验证证据写入 Trace。

## 状态

- 初赛：完成接口、权限、失败处理和迁移方案设计。
- 复赛：接入真实 SLS Project/Logstore，提供可回放查询证据。

## 调用契约

```json
{
  "project": "mergepilot-demo",
  "logstore": "ci-runtime",
  "query": "trace_id:mp-* and level:(ERROR or WARN)",
  "from_time": 0,
  "to_time": 0,
  "trace_id": "mp-20260722-001"
}
```

返回：

```json
{
  "logs": [],
  "matched_count": 0,
  "query_window": {"from": 0, "to": 0},
  "evidence_uri": "sls://project/logstore/query-id"
}
```

## 权限边界

- 使用仅允许目标 Project/Logstore 查询的 RAM 只读权限。
- 禁止删除日志、修改索引或跨租户查询。
- 输出前脱敏 AccessKey、Token、Cookie 和私钥片段。
- Skill 调用记录 `trace_id`、查询窗口、参数摘要、结果数量和失败原因。

## 失败与降级

1. 查询超时：缩小时间窗口并最多重试 2 次。
2. SLS 不可达：读取 CI 产物日志，证据状态标为 `partial`。
3. 权限不足：停止自动合并并升级人工处理，不尝试扩大权限。
4. 返回日志缺少 TraceId：仅作为辅助证据，不作为自动合并依据。

## 可替换性

Skill 上层只依赖统一的 `LogEvidenceQuery` Schema。替换为 Elasticsearch、Loki 或云厂商日志服务时，仅需更换协议适配器，不改变 Verifier 的验证流程。