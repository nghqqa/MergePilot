# M3-B1 证据 · 封闭旁路 + 角色 token 认证

> **direct-bridge bypass closed · role-token auth verified · audit capability wired (fail-open in B1)**
> 验证日期:2026-07-26(token 轮换 + 证据脱敏后闭合)

## 架构(B1 后)

```
reviewer   ─┐                                  ┌→ github-mcp(持 PAT)
fixer      ─┼─ mcporter + Bearer <role-token> ─→│  仅在 mcp-backend-net
verifier   ─┘  http://policy-gw:8083/<role>/sse │
                          ↓                      │
                  Policy Gateway(hiclab-net + mcp-backend-net)
                          ↑ Authorization Bearer
                  身份 = token,路径只声明意图;不符 → 401
```

## 已落地的安全属性

1. **直连 bridge 旁路封闭**:`github-mcp` 从 `hiclab-net` + `hiclaw-net` 摘除,仅留 `mcp-backend-net`;worker 不与 bridge 共网。reviewer 直连 `github-mcp:8082` → DNS 解析失败(`BYPASS_CLOSED`)。
2. **角色 token 认证(非 URL 路径)**:每个调用方持独立 opaque Bearer token(43 字符 `secrets.token_urlsafe(32)`)。token→role 映射只在 gateway env。路径 `/{role}/sse` 仅声明意图,必须与 token 一致。
3. **路径/token 一致性**:reviewer token 打 `/coordinator/sse` → `401 ROLE_PATH_MISMATCH`(防 worker 绕配置冒充 coordinator)。
4. **coordinator token 隔离**:coordinator token 仅在 `role-tokens.json`(host)+ gateway env,**未部署到任何 worker**;B4 才交给 Controller。
5. **审计能力已接入(fail-open)**:每次 `list_tools`/`call_tool`/auth-fail 都**尝试**写 `audit-pg.mcp_calls`;表本身 INSERT-only(UPDATE/DELETE 被触发器拒)。**注意 B1 的 fail-open 语义**:审计写入自身故障时(如 audit-pg 不可达)只记 stderr,**不阻断业务调用**——因此不宣称"任何情况下所有调用必然审计"。**B3/B4 将对写操作和 L2 动作改为审计不可用时 fail-closed**(读操作保持 fail-open 以兼顾可用性)。
6. **fail-closed(认证)**:token 缺失/无效 → 401 BAD_TOKEN;路径与 token 不符 → 401 ROLE_PATH_MISMATCH。

## 关键修正(踩坑)

| # | 问题 | 修复 |
|---|---|---|
| ① | 安全假设错误:原方案"身份=URL路径",但 worker=root、mcporter.json 0644 可改、mcporter 可直连任意 URL、worker 与 bridge 同网 → `/reviewer/sse` 可绕过 | 改为 Bearer token + 私有 mcp-backend-net 网络隔离(token 是不可伪造 secret,bridge 物理不可达) |
| ② | gateway.py 连接缓存变量 `_audit_conn` 与函数 `_audit_conn()` 同名 → def 重绑成函数对象,`if var is None` 永远 False,审计静默失败 | 连接缓存改名 `_audit_db`,函数 `_get_audit_conn()` |
| ③ | 割接脚本只挂 `policy-gw` 到 mcp-backend-net,漏挂 `github-mcp` → 摘 worker 网后 bridge 零网络,gateway 上游连不上 | run + cutover 脚本都把 `github-mcp` 挂进 mcp-backend-net(割接前置) |
| ④ | mcp SDK `SseServerTransport` 默认开 DNS rebinding 防护,按 Host 白名单拦内部主机名 `policy-gw:8083` | `TransportSecuritySettings(enable_dns_rebinding_protection=False)`(内部网络 + 自带 token 认证,可接受) |
| ⑤ | **凭证泄露**:B1 测试 C 段把临时 mcporter.json 原样 `cat` 到输出,reviewer token 明文进了证据文件 | ① 测试脚本 `cat` 加 `sed 's/Bearer <tok>/Bearer <REDACTED>/'` ② 轮换全部 4 个 token(`--force`)③ 删旧证据重跑 ④ grep 扫描确认无 `Bearer [A-Za-z0-9_-]{20,}` 残留 |

## 验证结果

### 非破坏性测试(B1 网关侧,7/7 PASS)— `b1-non-destructive-test.txt`
- 无 token → 401 BAD_TOKEN ✅
- 跨角色 token → 401 ROLE_PATH_MISMATCH ✅
- reviewer 经 gateway list → 完整工具集(5/5 已知工具命中)✅
- reviewer 调 get_me → 返回 login=nghqqa ✅
- 审计 ALLOW=3 + DENY=2 ✅
- mcp_calls UPDATE 被触发器拒 ✅

### 网络割接测试(7/7 PASS)— `b1-cutover-test.txt`
- reviewer 直连 github-mcp:8082 → BLOCKED(DNS fail)✅
- reviewer 经生产 mcporter.json 调 get_me → OK ✅
- fixer + verifier 各自 token → OK ✅
- github-mcp 不在 hiclab-net / 不在 hiclaw-net / 在 mcp-backend-net ✅

## 网络拓扑(B1 后)— `network-state.txt`

```
mcp-backend-net 成员: policy-gw + github-mcp
github-mcp 网络:      仅 mcp-backend-net
worker → github-mcp:  BYPASS_CLOSED(DNS resolution fail)
worker → gateway:     OK(经 hiclab-net)
gateway → github-mcp: OK(经 mcp-backend-net)
```

## 审计样本 — `mcp_calls-audit.txt`

```
reviewer | get_me       | ALLOW | B1_PERMISSIVE_CALL
reviewer | get_me       | ALLOW | UPSTREAM_RESULT
reviewer | (list_tools) | ALLOW | B1_PERMISSIVE_LIST
reviewer | (auth)       | DENY  | ROLE_PATH_MISMATCH
path=reviewer | (auth)  | DENY  | BAD_TOKEN
UPDATE mcp_calls → ERROR: mcp_calls is INSERT-only (immutable audit)
```

## 文件清单

| 文件 | 内容 |
|---|---|
| gateway.py + Dockerfile | Policy Gateway(MCP SDK server + Bearer auth + 不可变审计) |
| m3b_policy.sql | mcp_calls(不可变)+ approvals + policy_action_outbox(B4 用) |
| m3b-generate-tokens.sh | 生成 4 角色 token(chmod 600,不回显) |
| run-policy-gateway.sh | 起 gateway(双网络)+ 应用 schema |
| m3b-cutover-isolation.sh | 破坏性割接:repoint worker + 摘 bridge 旁路 |
| m3b-cutover-rollback.sh | 应急回滚 |
| m3b-b1-test.sh | 非破坏性 7 项验证 |

## 范围说明

B1 已闭合:`m3b-b1-closed` 标签。落地项 = **直连旁路封闭 + 角色 token 认证 + path/token 一致性 + INSERT-only 审计表(触发器防篡改)+ 审计写入接入(fail-open)**。

**B1 的已知边界(后续阶段补)**:
- 审计 fail-open:审计写入故障时不阻断业务。写操作/L2 动作的 fail-closed-on-audit-failure → **B3/B4**。
- 工具级权限矩阵:此刻 reviewer 认证后仍可调 merge(B1 permissive)→ **B2** 接 policy.yaml 做 deny-by-default 过滤。
- L2 审批票据:coordinator token 已隔离但 merge 尚无票据门禁 → **B4**。
- 负向证据全集(8 项)→ **B5**。
