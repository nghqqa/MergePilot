# Authentication and Allowlist

## 会话模型
- 服务端内存会话（重启即失效，诚实行为）
- Cookie `mp_session`（HttpOnly、SameSite=Strict、HMAC 签名）
- Cookie `mp_csrf`（非 HttpOnly，用于 CSRF 校验）
- TTL 默认 8 小时（可通过 CONSOLE_SESSION_TTL_MS 配置）

## 登录流程
1. `POST /api/auth/login {user, password}`
2. 服务端 timing-safe 凭据比对
3. 成功：设置 mp_session + mp_csrf cookies
4. 失败：401（不区分"用户不存在"和"密码错误"）

## CSRF
- 所有副作用方法（POST/PUT/PATCH/DELETE）必须携带 `X-CSRF-Token` 头
- 缺失或不匹配 → 403

## 仓库 Allowlist
- `CONSOLE_REPO_ALLOWLIST` 环境变量（逗号分隔）
- **服务端行级过滤**：不在 allowlist 中的仓库数据不可见
- `?repo=` 参数越界 → 403 `repo_not_in_allowlist`
- 未知资源 → 404（不泄露存在性）

## 边界
- 401：未认证
- 403：认证但越权
- 404：资源不存在
- 400：请求格式错误
- 500：服务器内部错误（真实故障注入时验证）
