# RAG 生产依赖运维 Runbook（DEPENDENCY_READINESS 波）

> 本文档只描述接口与流程，不包含任何真实凭据。所有生产值由组织侧受控注入。

## 1. Keystore 生命周期

### 分发（首次部署）
```
组织侧 secret manager（Vault/AWS SM/etc）
  ↓ 安全通道（加密卷 / Docker Secret / k8s Secret）
  ↓ 写入容器挂载路径（如 /data/keystore/）
  ↓ 文件：<key_id>.key.json，权限 0600，目录 0700
  ↓ console 启动时 loadKeys() 读取
```

### 验证分发成功
```bash
# 在 console 容器内：
curl -s -H "cookie: $SESSION" /api/cchain/status | jq '.components[] | select(.component=="run_binding_auth")'
# 期望 state=READY, key_count>=1
```

### 轮换
1. 在 secret manager 生成新密钥 → 写入新 .key.json（旧文件保留）
2. 验证新密钥可验签：`POST /api/rag-trial/machine/query`（用新密钥签名）
3. 验证旧密钥仍可验签（宽限期）
4. 宽限期结束后：更新旧 .key.json `"revoked": true`
5. 验证旧密钥被拒：`401 RUN_BINDING_AUTH_BLOCKED` 或 `BAD_SIGNATURE`

### 撤销（紧急）
1. 直接设置 `"revoked": true`（或删除 .key.json）
2. 效果立即（下次 loadKeys 即过滤）
3. 审计记录：已有 RUN_BINDING_VERIFY_DENIED 事件

### 恢复
1. 从 secret manager 重新写入 .key.json
2. 重启 console（或等待 loadKeys 下次调用——当前实现为每请求重新读取）

### 失败模式
| 状态 | 触发条件 | API 表现 |
|---|---|---|
| NOT_CONFIGURED | env 未设 | cchain/status → BLOCKED |
| MISSING/not_distributed | 目录不存在或无有效密钥 | cchain/status → BLOCKED |
| 验签拒绝 | revoked/expired/BAD_SIGNATURE/REPLAYED_NONCE | 401 + 具体原因 |

## 2. External Attestation 生命周期

### 接入
1. 组织侧部署 attestation 服务（或使用第三方）
2. 服务返回 JSON：`{provider, model, attestation: {digest...}, key_id}`
3. 配置 `MERGEPILOT_PROVIDER_ATTEST_URL` + `MERGEPILOT_PROVIDER_EXPECTED_KEY_ID`

### 验证接入成功
```bash
curl -s -H "cookie: $SESSION" /api/cchain/status | jq '.components[] | select(.component=="provider_attestation")'
# 期望 state=ATTESTED
```

### 失败模式
| 状态 | 触发条件 | API 表现 |
|---|---|---|
| NOT_CONFIGURED | URL 未设 | cchain/status → BLOCKED |
| UNREACHABLE | 不可达/非 200/超时 | cchain/status → BLOCKED |
| INVALID | 非 JSON/缺字段/key_id 不匹配 | cchain/status → BLOCKED |

### 故障处置
- attestation 服务不可达 → console 自动 BLOCKED（fail-closed），不降级
- 恢复后自动重新探测（每次 /api/cchain/status 实时调用）
- key_id 更换 → 更新 `MERGEPILOT_PROVIDER_EXPECTED_KEY_ID` env → 重启 console

## 3. promote3 切换前检查清单

- [ ] `main` CI 全绿（3 工作流 completed/success）
- [ ] 候选镜像 digest 确认（本地 = 远端）
- [ ] Trivy HIGH/CRITICAL = 0（fresh DB）
- [ ] SBOM 生成并存档
- [ ] staging 栈全链验证通过（R@5 ≥ 0.90/引用 100%/五态/边界）
- [ ] keystore state=READY（cchain/status）
- [ ] attestation state=ATTESTED（cchain/status）
- [ ] C 链 enforcement=off（除非另有授权）
- [ ] PG/MinIO 备份已执行
- [ ] 回滚锚点确认（当前运行 digest + 上一验证 digest + 6h 基线）
- [ ] 回滚步骤演练通过

## 4. promote3 回滚步骤

```bash
cd /path/to/promote-runtime
# 1. 修改 compose 中 console image 行为回滚锚点 digest
# 2. docker compose up -d console（数据卷向后兼容）
# 3. 验证 health + /api/health + 前端可达
# 4. 验证 RAG/模型状态（回滚镜像可能无 /api/rag-trial/* 端点——属预期）
```

## 5. 组织侧待提供清单

| # | 项 | 说明 | 优先级 |
|---|---|---|---|
| 1 | Secret manager 地址与访问策略 | 用于创建/分发 RUN_BINDING 密钥 | P0（阻塞） |
| 2 | 生产 key ID 列表 | 每个需要访问 RAG 机器端点的 worker/run 的 key_id | P0 |
| 3 | Attestation 服务 URL | 返回 provider/model/attestation JSON | P0（阻塞） |
| 4 | Expected key ID | attestation 响应中 key_id 的预期值 | P0 |
| 5 | 证书/信任根（如 attestation 使用 TLS） | CA 证书或信任链 | P1 |
| 6 | 轮换责任人 | 定期轮换执行人+审批人 | P1 |
| 7 | 撤销责任人 | 紧急撤销授权人 | P1 |
