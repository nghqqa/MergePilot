# Webhook 事件接入部署手册（mergepilot.nghqqa.cn · 腾讯云）

> 目标（Phase 1）：`PR 事件 → GitHub webhook → HTTPS 端点 → HMAC 验签 → 入库 PENDING`。
> 不含 Agent 调度与结果回写（Phase 2/3，见文末预告）。
> 全部组件复用仓库现有实现：`tools/gh-app/`（接收/验签/去重/入库）、
> `docker-compose.yml` 的 `postgres` + `gh-webhook` 服务、`release/offline/db-init/` 表结构。

## 架构

```
GitHub ──HTTPS(443)──> NGINX(mergepilot.nghqqa.cn) ──proxy_pass──> 127.0.0.1:8090
                                                                    gh-webhook(容器)
                                                                        │ isolated 网络
                                                                        ▼
                                                                   postgres:5432
                                                              (github_deliveries 表, 不发布端口)
```

- 路由：`POST /webhook`（事件入口）、`GET /healthz`（活性检查）
- 语义：新 PR 事件 `202 PENDING`；重复交付 `200 duplicate`；验签失败 `401`（零写入）

## 前置确认

| 项 | 状态 | 说明 |
| --- | --- | --- |
| ICP 备案 | 已具备 | nghqqa.cn 博客与 HTTPS 已在腾讯云运行 |
| 服务器 | 待确认 | Linux x86_64，剩余 ≥1C2G 即可（本阶段只有 PG+接收端） |
| Docker | 待安装/确认 | ≥ 20.10，compose v2 |
| DNS 权限 | 待操作 | 腾讯云 DNSPod 控制台 |
| **CDN** | **注意** | `mergepilot.nghqqa.cn` 必须**直连源站 IP，不挂 CDN**（webhook 是 POST，不需要缓存层，CDN 还会干扰排障） |

## S1 · DNS 解析

DNSPod 添加 A 记录：`mergepilot.nghqqa.cn → <服务器公网IP>`，线路默认，TTL 600。
验证：`ping mergepilot.nghqqa.cn`（应返回服务器 IP，而非 CDN 节点）。

## S2 · 服务器装 Docker

```bash
# 先确认是否已装
docker -v || curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
systemctl enable --now docker
docker compose version   # 确认 v2
```

> 本机实测记录（腾讯云 2C2G · CentOS）：内存 available 1.3G、已有 swap 2G（无需再加）、
> 磁盘剩余 24G——满足 Phase 1/2 全部要求。

## S3 · 镜像上传（推荐：本机导出，规避国内拉取 GitHub/DockerHub 的不确定性）

在**本机**（仓库与 Docker 都在）执行：

```bash
cd <仓库根>
docker build -t mergepilot-isolated-gh-webhook:local -f Dockerfile.gh-webhook .
docker pull pgvector/pgvector:pg16
docker save pgvector/pgvector:pg16 mergepilot-isolated-gh-webhook:local \
  | gzip > mp-webhook-images.tar.gz
scp mp-webhook-images.tar.gz root@<服务器IP>:/opt/mergepilot/
```

服务器上加载并传代码（只需少量文件）：

```bash
mkdir -p /opt/mergepilot && cd /opt/mergepilot
docker load < mp-webhook-images.tar.gz
# 从本机 scp 三个东西：docker-compose.yml、release/offline/db-init/、config/
scp -r docker-compose.yml release config root@<服务器IP>:/opt/mergepilot/
```

## S4 · 密钥与 env 文件（全部留在服务器，不进 git）

```bash
cd /opt/mergepilot
# 生成三个随机密钥并记录
openssl rand -hex 32   # → PG_PASS
openssl rand -hex 32   # → WEBHOOK_SECRET（S7 注册 GitHub webhook 时用同一个）
openssl rand -hex 24   # → INGRESS_PASS

cat > postgres.env <<EOF
POSTGRES_USER=mergepilot
POSTGRES_PASSWORD=<PG_PASS>
POSTGRES_DB=mergepilot_audit
EOF

cat > gh_webhook.env <<EOF
GITHUB_WEBHOOK_SECRET=<WEBHOOK_SECRET>
GITHUB_INGRESS_DSN=postgresql://github_event_ingress:<INGRESS_PASS>@postgres:5432/mergepilot_audit
EOF

cat > .env <<EOF
MERGEPILOT_RUN_ID=webhook-ingress-phase1
MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=unused-phase1
EOF

chmod 600 postgres.env gh_webhook.env .env
```

> 说明：`MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES` 属于 demo-console 服务（Phase 1 不启动），
> 但 compose 的 `${VAR:?}` 插值是**整文件生效**的——缺了它，哪怕只起 postgres/gh-webhook
> 也会在解析阶段报错，所以必须占位。

修改 `docker-compose.yml` 的 `gh-webhook.ports`：`"0.0.0.0:8090:8090"` → `"127.0.0.1:8090:8090"`（只允许 NGINX 反代访问）。

## S5 · 起库、初始化表结构、起接收端

```bash
# compose 对 env_file 的存在性做全文件校验——未启动的服务（controller/demo-console/
# preflight）引用的 env 也必须存在；Phase 1 用空占位（这些服务不会被启动）
touch controller.env demo_console.env && chmod 600 controller.env demo_console.env

docker compose up -d postgres
sleep 8
# 按序执行 db-init（000 角色 → 001 表 → 002 console → 003 登录账号 → 004 环境 → 005 种子）
for f in release/offline/db-init/*.sql; do
  docker compose exec -T postgres psql -U mergepilot -d mergepilot_audit -v ON_ERROR_STOP=1 < "$f"
done
# 003-logins.sql 内置的是 smoke 密码——公网部署必须改掉：
docker compose exec postgres psql -U mergepilot -d mergepilot_audit \
  -c "ALTER ROLE github_event_ingress PASSWORD '<INGRESS_PASS>';"

docker compose up -d gh-webhook
curl -s http://127.0.0.1:8090/healthz   # 期望 {"ok":true,...}
```

## S6 · TLS 证书 + NGINX 反代

> CentOS 注意：老版本（7 系已 EOL）yum 源指向 vault，装包易失败。**推荐用 docker 版
> certbot（零主机依赖，任何发行版一致）**；若系统较新也可 `dnf install certbot python3-certbot-nginx`。

```bash
# 1) NGINX server block：80 端口 + webroot（供证书验证）
mkdir -p /var/www/certbot
cat > /etc/nginx/conf.d/mergepilot-webhook.conf <<'EOF'
server {
    listen 80;
    server_name mergepilot.nghqqa.cn;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
EOF
nginx -t && systemctl reload nginx

# 2) docker 版 certbot 签发（webroot 模式）
docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/lib/letsencrypt:/var/lib/letsencrypt \
  -v /var/www/certbot:/var/www/certbot certbot/certbot certonly --webroot -w /var/www/certbot \
  -d mergepilot.nghqqa.cn --email <你的邮箱> --agree-tos --no-eff-email

# 3) HTTPS server block
cat > /etc/nginx/conf.d/mergepilot-webhook.conf <<'EOF'
server {
    listen 80;
    server_name mergepilot.nghqqa.cn;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 443 ssl;
    server_name mergepilot.nghqqa.cn;
    ssl_certificate     /etc/letsencrypt/live/mergepilot.nghqqa.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mergepilot.nghqqa.cn/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
nginx -t && systemctl reload nginx
curl -s https://mergepilot.nghqqa.cn/healthz   # 公网验证
```

> 续期：`docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/lib/letsencrypt:/var/lib/letsencrypt \
> -v /var/www/certbot:/var/www/certbot certbot/certbot renew --webroot -w /var/www/certbot && nginx -s reload`
> 加入 crontab 每月执行一次即可。

## S7 · GitHub 注册 webhook

目标仓库（先用 `nghqqa/fastapi-boilerplate-demo`）→ **Settings → Webhooks → Add webhook**：

| 字段 | 值 |
| --- | --- |
| Payload URL | `https://mergepilot.nghqqa.cn/webhook` |
| Content type | `application/json` |
| Secret | S4 的 `<WEBHOOK_SECRET>` |
| Events | **Pull requests**（勾选后 GitHub 会自动发一条 ping） |

注册后立即看 **Recent Deliveries**：ping 应返回 `200`（接收端按合同归为 IGNORED）。

## S8 · 端到端验证

1. 在试点仓库开一个测试 PR（或 push 新 commit 触发 synchronize）。
2. GitHub Recent Deliveries：期望 `202`。
3. 服务器查库：

```bash
docker compose exec postgres psql -U mergepilot -d mergepilot_audit \
  -c "SELECT delivery_id, event, action, status, received_at FROM github_deliveries ORDER BY received_at DESC LIMIT 5;"
```

期望新行 `status = PENDING`。
4. 验签反向测试：临时把 GitHub webhook secret 改错一位、重发 delivery → 期望 `401`，库中无新行。改回正确 secret。

## S9 · 安全清单

- [ ] 三个 secret 只存在于服务器 env 文件（600 权限），不进 git、不进聊天记录
- [ ] 8090 端口仅绑 127.0.0.1，公网唯一入口是 NGINX 443
- [ ] postgres 无对外端口（compose 默认不发布，保持）
- [ ] `mergepilot.nghqqa.cn` 不在 CDN 加速名单
- [ ] certbot 自动续期：`systemctl list-timers | grep certbot`

## 实测记录（Phase 1 闭环 · 2026-09-19）

端到端验证于 2026-09-19T04:10:34Z 完成：真实 `pull_request/synchronize` 事件
（fastapi-boilerplate-demo PR #4）→ HTTPS → HMAC 验签 → `github_deliveries`
PENDING 入库 → HTTP **202**；ping 按合同 IGNORED。测试 PR 验证后已关闭留痕。

首次真实流量暴露并修复了三处 P14 遗留问题（均已在仓库提交）：

1. `github_deliveries.event_name` CHECK 白名单与接收端写原始事件名冲突（push 事件 503）→ 约束放宽为接收端同款正则；
2. 接收端 `classify()` 强制 `installation_id`——repo webhook 载荷无此字段（400）→ 不再必需（Phase 2 接 GitHub App 后自然填充）；
3. `gh_deliveries_pull_request_envelope` 约束同样要求 installation 非空（503）→ 放开该字段，其余校验保留。

结论：入口实现此前从未被真实 GitHub 事件验证过，本日链路为首个真实流量验证。

## 实测记录 · Phase 2（出口链路 · 2026-09-19）

组件：`mp-checks-reporter` 容器（复用 gh-webhook 镜像，入口换成 `checks_reporter.py`），
轮询 `github_check_outbox` → GitHub Checks API 发布 check run。

- GitHub App：`MergePilot-Reporter`（App ID 4997459 · Installation 162911455 ·
  repo 1348534810=nghqqa/fastapi-boilerplate-demo）；权限仅 Checks RW，
  App webhook 关闭（入口由 repo webhook 承担，避免双投递）。
- 网络：发布器是栈内唯一需要出网的容器——挂 `mp-reporter-egress`（普通桥接，
  出网）+ `isolated`（连 postgres）；其余服务维持 `internal: true` 全隔离。
- 私钥：宿主 `secrets/github-app-private-key.pem`，属主 9090:9090（容器运行用户）、
  600，容器内只读挂载到冻结路径 `/run/secrets/github-app-private-key.pem`。
- 认证链实测：PEM→RS256 JWT→installation token（scoped checks:write）→
  API 200 ✓；Checks API 写路径 POST check-run 201 ✓。

Phase 2 修复（真实 api.github.com 首跑暴露，已提交）：
`token_provider` 换取 installation token 只认 200，而 GitHub 该端点成功码为
**201 Created**——成功被当终局失败。修为接受 200/201（tests 816 passed）。

今日累计：P14 的 GitHub 出入口组件共修复 4 处"从未被真实流量验证"的缺陷
（event_name 约束 / classify 必填 installation_id / 信封约束 / 201 成功码）。

## Phase 2 预告 · GitHub App + 结果回写

复用已实现的 `tools/gh-app/token_provider.py`（App JWT/installation token）与
`checks_reporter.py`（检查运行/评论回写）：创建 GitHub App（权限仅
`Pull requests: Read & write` + `Contents: Read-only`），`.pem`/App ID/Installation ID
进服务器 env。回写的是**审查结论与建议**，不开任何写 contents 权限——合并决策仍归维护者。

## Phase 3 预告 · Controller → AgentTeams 调度

复用 `tools/workflow-controller/controller.py`（消费 PENDING 交付、生成任务提交）。
AgentTeams 全栈仍在本地时，经 Tailscale/frp 隧道触发；服务器规格充足后再整体迁移。
