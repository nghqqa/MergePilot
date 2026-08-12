# M6-C · 真实 SLS 生产接入设计与 Preflight

> 状态：**设计冻结 + 离线 preflight 已验证**（offline stub signer, no real SLS）。
> 分支：`feat/m6c-sls-production`（基于 main@73d6fe9）
> 冻结日期：2026-08-11
> 前置：M6-A tag `m6a-otel-local-collector-closed`，M6-B tag `m6b-sls-local-vertical-closed`

---

## 1. 生产 SLS 接入契约（冻结）

### 1.1 Endpoint

```
SLS_ENDPOINT=https://{project}.{region}.sls.aliyuncs.com
SLS_PROJECT=mergepilot-trace
SLS_LOGSTORE=mp-trace
```

- Region: 由部署环境决定（如 `cn-hangzhou`）。
- Protocol: HTTPS only。
- Path: `/logstores/{logstore}/shards/lb`（SLS PutLogs API）。

### 1.2 Trace 字段映射（复用 M6-B §2，不重复）

所有 `mp.*` → `tags.mp_*` 映射不变。新增生产专用字段：

| 字段 | 说明 |
|---|---|
| `env` | `prod` / `test` / `staging` |
| `deploy_commit` | 当前部署的 git HEAD |
| `sls_exporter_version` | `m6c-v1` |

### 1.3 保留策略

| 数据类型 | 保留期 | SLS 配置 |
|---|---|---|
| 正常 Trace (status_code=1) | 7 天 | logstore TTL=7 |
| 错误 Trace (status_code=2) | 30 天 | 独立 logstore `mp-trace-error` TTL=30 |

### 1.4 批量/重试/超时/背压（复用 M6-B §3，不重复）

全部参数复用 M6-B 冻结值。生产唯一新增约束：
- `total_export_budget_ms` 在生产中调为 **3000**（从 6000 降低，避免 L2 drain 积压）。

---

## 2. 最小权限 RAM 策略（冻结）

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["log:PostLogStoreLogs"],
      "Resource": [
        "acs:log:*:*:project/mergepilot-trace/logstore/mp-trace",
        "acs:log:*:*:project/mergepilot-trace/logstore/mp-trace-error"
      ]
    }
  ]
}
```

- **仅允许 PostLogStoreLogs**（写入）。
- **禁止**：GetLogs（读）、DeleteLogStore（删除）、CreateIndex（管理）、ListLogStores（枚举）。
- AK/SK 绑定到此 RAM 角色，不可用于其他 SLS 操作。

---

## 3. 凭据注入与 60 秒热轮换（冻结）

### 3.1 凭据来源

| 凭据 | 环境变量 | 说明 |
|---|---|---|
| AccessKey ID | `SLS_ACCESS_KEY_ID` | RAM 子账号 AK |
| AccessKey Secret | `SLS_ACCESS_KEY_SECRET` | RAM 子账号 SK |
| Credential File | `SLS_CREDENTIAL_FILE` | 可选：JSON 文件路径，60s 热轮换 |

### 3.2 热轮换

- `SLS_CREDENTIAL_FILE` 指向一个 JSON 文件：
  ```json
  {"access_key_id": "LTAI...", "access_key_secret": "abc..."}
  ```
- `CredentialProvider` 每 60 秒重新读取文件。
- 文件 mtime 变化 → 立即重新加载（不等待 60s）。
- 凭据值**永不**出现在 span 属性、日志、SLS payload 或代码中。
- 凭据轮换不需要重启 MergePilot 进程。

### 3.3 SLS 请求签名（离线 stub 验证）

SLS PutLogs API 使用 HMAC-SHA1 签名：
```
Authorization: LOG <AccessKeyId>:<Signature>
Signature = HMAC-SHA1(AccessKeySecret, StringToSign)
StringToSign = "POST\n"
             + MD5(body) + "\n"
             + "application/json\n"
             + "x-sls-bodyrawsize:" + len(body) + "\n"
             + "x-sls-apiversion:0.6.0\n"
             + "x-sls-signaturemethod:hmac-sha1\n"
             + Date + "\n"
             + "x-sls-host:" + host
```

**离线验证方式**：
- `SlsStubSigner`：用固定 AK/SK 生成签名，验证签名格式正确。
- `FakeSLSReceiver`（M6-B）：接收带签名的请求，验证 header 存在且格式正确。
- **不发送真实请求到 SLS**。

---

## 4. 生产窗口 Runbook

### 4.1 Pre-flight 检查

```bash
# 1. 验证凭据已注入（不输出值）
test -n "$SLS_ACCESS_KEY_ID" && echo "AK present" || echo "AK MISSING"
test -n "$SLS_ACCESS_KEY_SECRET" && echo "SK present" || echo "SK MISSING"
test -n "$SLS_ENDPOINT" && echo "endpoint present" || echo "endpoint MISSING"

# 2. 运行离线 preflight
python3 tests/otel/test_sls_production_preflight.py

# 3. 运行 79 项回归
python3 -m pytest tests/otel/ -q

# 4. 验证 MergePilot 状态机正常
# (通过正常的 Controller → Gateway → Skill 流程)
```

### 4.2 启动 SLS 导出

```bash
# 在 Controller 部署中设置
export SLS_ENDPOINT=https://mergepilot-trace.cn-hangzhou.sls.aliyuncs.com
export SLS_PROJECT=mergepilot-trace
export SLS_LOGSTORE=mp-trace
export SLS_ACCESS_KEY_ID=<from secret manager>
export SLS_ACCESS_KEY_SECRET=<from secret manager>
# 可选热轮换
export SLS_CREDENTIAL_FILE=/etc/mergepilot/sls-creds.json
```

### 4.3 验证 trace 到达

```python
# 在 MergePilot 运行一次最小 PR 流程后
# 使用 SLS 控制台或 API 查询：
# __topic__: "mergepilot-trace"
# run_id: <known-run-id>
# 预期：5-10 spans，全部 status_code=1
```

### 4.4 Rollback

如果 SLS 导出出现问题：

1. **移除 env 变量** → SLSExporter 自动降级为 no-op（fail-closed）。
2. **业务不受影响**：状态机、审计、所有 PR 流程正常继续。
3. **检查 SLS 配额/权限** → 重新注入凭据。
4. **不需要回滚代码**：OTel instrumentation 始终存在，SLS 仅是 sink。

### 4.5 告警规则

| 告警 | 条件 | 严重度 |
|---|---|---|
| SLS export 失败率 > 50% | `failed_exports / (failed + exported) > 0.5` 在 5 分钟窗口 | WARNING |
| Span 丢弃 > 0 | `dropped_batches > 0` | WARNING |
| SLS 完全不可达 | `exported_spans == 0` 在 10 分钟窗口且 Controller 活跃 | CRITICAL |
| 凭据过期 | HTTP 403 连续出现 | CRITICAL |

---

## 5. 离线 Preflight 测试（不接触真实 SLS）

### 测试覆盖

| 类别 | 测试 |
|---|---|
| 签名格式 | HMAC-SHA1 签名正确生成；header 格式合规 |
| 凭据注入 | env 注入 → CredentialProvider 读取；不输出值 |
| 热轮换 | 文件 mtime 变化 → 立即重载；60s 定期重载 |
| 请求 schema | SLS PutLogs JSON 格式正确 |
| 脱敏 | PAT/token/secret → `<redacted>` 在签名前 |
| Fail-closed | 无凭据 → no-op；不阻塞业务 |
| 向后兼容 | M6-A 57 + M6-B 79 项全通过 |

---

## 6. 不在本轮范围

- 真实 SLS 连接（需 operator 凭据 + 生产窗口授权）
- Nacos / RocketMQ
- 生产 HiClaw trace 采集
- Metrics（仅 Trace）
- SLS 告警规则的 SLS 控制台配置
