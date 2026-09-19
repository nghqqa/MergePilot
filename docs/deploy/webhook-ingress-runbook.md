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
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
systemctl enable --now docker
docker compose version   # 确认 v2
```

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
EOF

chmod 600 postgres.env gh_webhook.env .env
```

修改 `docker-compose.yml` 的 `gh-webhook.ports`：`"0.0.0.0:8090:8090"` → `"127.0.0.1:8090:8090"`（只允许 NGINX 反代访问）。

## S5 · 起库、初始化表结构、起接收端

```bash
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

```bash
apt install -y certbot python3-certbot-nginx   # 按服务器包管理器调整

# 先建 80 端口 server block 供 certbot 验证
cat > /etc/nginx/conf.d/mergepilot-webhook.conf <<'EOF'
server {
    listen 80;
    server_name mergepilot.nghqqa.cn;
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

certbot --nginx -d mergepilot.nghqqa.cn   # 自动改写 443 + 续期
curl -s https://mergepilot.nghqqa.cn/healthz   # 公网验证
```

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

## Phase 2 预告 · GitHub App + 结果回写

复用已实现的 `tools/gh-app/token_provider.py`（App JWT/installation token）与
`checks_reporter.py`（检查运行/评论回写）：创建 GitHub App（权限仅
`Pull requests: Read & write` + `Contents: Read-only`），`.pem`/App ID/Installation ID
进服务器 env。回写的是**审查结论与建议**，不开任何写 contents 权限——合并决策仍归维护者。

## Phase 3 预告 · Controller → AgentTeams 调度

复用 `tools/workflow-controller/controller.py`（消费 PENDING 交付、生成任务提交）。
AgentTeams 全栈仍在本地时，经 Tailscale/frp 隧道触发；服务器规格充足后再整体迁移。
