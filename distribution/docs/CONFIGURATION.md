# Configuration and Secrets Management

## 环境变量

### 必填（无默认值）
| 变量 | 说明 | 生成方式 |
|---|---|---|
| PG_PASSWORD | PostgreSQL 密码 | `openssl rand -hex 16` |
| MINIO_USER | MinIO 用户名 | `openssl rand -hex 8` |
| MINIO_PASSWORD | MinIO 密码 | `openssl rand -hex 16` |
| CONSOLE_USER | 操作员用户名 | 自定 |
| CONSOLE_PASSWORD | 操作员密码 | `openssl rand -hex 12` |
| SESSION_SECRET | 会话签名密钥 | `openssl rand -hex 24` |
| REPO_ALLOWLIST | 授权仓库列表 | 逗号分隔 |

### 可选（有默认值）
| 变量 | 默认 | 说明 |
|---|---|---|
| PG_DB | merg
