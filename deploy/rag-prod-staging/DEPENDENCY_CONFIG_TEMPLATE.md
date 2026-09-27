# ═══ 生产依赖配置模板（占位符，绝不填入真实值）═══
# 复制到实际 compose 文件的 environment 段后，由组织侧受控填入真实值。
# 以下所有值均为占位符/空值——不存在任何真实凭据。

# ── 1. RUN_BINDING keystore（secret manager 注入）──
# 组织侧操作：
#   1. 在受控 secret manager（如 Vault/AWS Secrets Manager）中创建 RUN_BINDING 密钥
#   2. 通过安全通道将密钥 JSON 写入 keystore 目录（NFS/Secret/加密卷挂载）
#   3. 确保目录权限：owner=root(或专用服务账号)，mode=0700；文件 mode=0600
# 密钥 JSON 格式（详见 console/backend/lib/cchain/index.mjs loadKeys）：
#   {
#     "key_id": "<由组织分配>",
#     "secret": "<HMAC-SHA256 密钥 hex，由 secret manager 生成>",
#     "algorithm": "hmac-sha256-full",
#     "created_at": "<ISO8601>",
#     "expires_at": "<ISO8601，建议 90 天>",
#     "revoked": false
#   }
MERGEPILOT_RUN_BINDING_KEYSTORE: "/data/keystore"  # 容器内路径；宿主侧由 secret manager 管理挂载

# ── 2. External attestation ──
# 组织侧操作：
#   1. 部署或指定 provider attestation 服务端点
#   2. 分配预期 key_id（与 attestation 响应中的 key_id 字段对应）
# 响应契约（fetchProviderAttestation 校验）：
#   HTTP 200 + JSON { "provider": "...", "model": "...", "attestation": {...}, "key_id": "..." }
MERGEPILOT_PROVIDER_ATTEST_URL: ""                    # ← 组织侧填入（如 https://attest.example.com/v1/attest）
MERGEPILOT_PROVIDER_EXPECTED_KEY_ID: ""               # ← 组织侧填入（如 attest-key-2026-q4）
MERGEPILOT_PROVIDER_TIMEOUT_MS: "5000"                # 超时毫秒（默认 5000）

# ── 3. C 链 enforcement（默认 off，需另行授权才可启用）──
MERGEPILOT_CCHAIN_ENFORCE: "0"                        # "1" = 启用（需三项 READY 后另行授权）

# ── fail-closed 语义（代码已实现，此为运维提醒）──
# keystore：目录不存在 / 无有效密钥 / 密钥全撤销或过期 → RUN_BINDING_AUTH_BLOCKED
# attestation：URL 未配置 / 不可达 / 非 200 / 非 JSON / 缺字段 / key_id 不匹配 / 超时 → BLOCKED
# 以上任一 BLOCKED → /api/cchain/status overall=BLOCKED → 不伪装 READY
