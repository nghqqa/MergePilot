# MergePilot v0.1 Preview — 架构与安全边界

版本 v0.1.0-preview.2 · git SHA 以 manifest.json 的 git_commit 为准

## 1. 组件拓扑

```
Windows 宿主机（loopback-only）
  └─ http://127.0.0.1:8600  ← console-edge（无秘密发布边，GET-only）
        └─ 固定上游 http://demo-console:8600（唯一常量，不可被请求影响）
              ├─ 静态页 /e2e-status.html（零第三方代码，CSP default-src 'none'）
              └─ /api/e2e/status（只读投影）
  └─ http://127.0.0.1:8090  ← gh-webhook（loopback 发布）

WSL2 发行版（MergePilot-Test）Docker
  ├─ console-edge        发布边：固定路径白名单、固定上游、无 DSN/密码
  ├─ demo-console        唯一挂载：.mergepilot/public → /run/mergepilot/public（ro）
  ├─ controller / policy-gateway / postgres   内部网络，无宿主端口
  └─ gh-webhook          loopback 发布

Ubuntu-22.04（独立发行版）
  └─ HiClaw（manager/controller/fixer/reviewer/verifier）+ Tuwunel Matrix
     —— 与控制台栈完全隔离；控制台不触碰、不修改其 live/canonical 配置
```

## 2. 数据流：单写者投影

```
E2E 生命周期（tools/cli/e2e_lifecycle.py，唯一写者）
  → session journal（含 relay 清理归属六字段、e2e_last_error 首个稳定错误）
  → write_session 每次持久化时派生 public_status_payload（19 键白名单，断言约束）
  → .mergepilot/public/status.json（宿主目录）
  → demo-console 只读挂载 → /api/e2e/status 原样服务
  → 前端只做呈现，绝不合成任何字段
```

- 白名单之外的字段（路径、argv、秘密邻接字段）**结构上不可能**进入投影。
- 客户端无法指定任意 journal/receipt 路径：状态文件路径仅来自部署期 server 配置。

## 3. 安全边界（全部有测试钉住）

| 边界 | 实现 |
|---|---|
| 只读 | 页面零写方法；edge POST/PUT/PATCH/DELETE/OPTIONS/TRACE/HEAD→405、CONNECT→403 |
| 路径 | 固定 frozenset 白名单；绝对形式 URI/反斜杠/控制字符→403 |
| Host | 仅 127.0.0.1/localhost，其它→403 |
| 头转发 | 客户端头零转发（Authorization/Cookie/Proxy-*/X-Forwarded-* 终结于此） |
| 缓存 | API `Cache-Control: no-store` |
| CSP | `default-src 'none'; base-uri 'none'; form-action 'none'; connect-src 'self'` |
| 第三方 | 零远程 src/href；图标全部自绘内联 SVG |
| 绑定 | 宿主侧仅 127.0.0.1:8600 / 127.0.0.1:8090；demo-console 无宿主端口 |
| 秘密 | 控制台栈零 DSN/密码挂载；secrets 目录不进入安装包 |
| 日志 | edge 只记 method+白名单路径+状态码，永不记头/秘密/正文 |

## 4. 诚实性契约

- `journal_complete` 为严格相等（`e2e_stage == "complete"`），不做推断；
- 缺失字段一律显示 `未提供`；未验证边界一律 `NOT_VERIFIED`；
- `direct_routing_verified=false` 永远显示 `false（经中继）`，绝不显示 VERIFIED；
- 路由边的 VERIFIED/FAIL 是**逐边探测结果**，与直连路由声明无关；
- 时间标签读投影自身 `updated_utc` 的年龄——冻结的 journal 会如实显示为逐渐变旧；
- 上游失联时保留最后良好数据并亮出 `陈旧` 裁决，恢复后自动回到真实状态。

## 5. 本 Preview 的明确限定

- `transport_profile=wsl-user-relay`：跨桥边经用户态 TCP 中继（绕过 WSL 6.18
  桥接 netfilter 缺陷），证据如实携带 `direct_routing_verified=false`；
- 五项真实性边界全部 false / NOT_VERIFIED，**部署或演示控制台不会使其翻转**：
  `application_integration_verified` / `database_verified` / `production_verified`
  / `revision_producer_contract` / `audit_producer_contract`；
- 本 Preview 是本地 staging/demo 级验证，**不是生产验证**。
