# A 链 reference-only 操作手册

适用：本机隔离 staging（127.0.0.1:48200），A 链 = worker rag_retrieve →
lexical-zh-en-v1 → 隔离 rag-live → 组织安全标准语料 → 检索审计。

## 启用方式

```bash
# 1. 启动 rag-live 词法服务（本机回环）
cd D:/goai/mp-worktrees/console
node verification/rag-org-knowledge/rag-live.mjs &

# 2. 重建 staging console 容器（同一镜像，追加 env）
STAGE_PW=$(docker inspect mp-stage-pg --format \
  '{{range .Config.Env}}{{println .}}{{end}}' | \
  grep '^POSTGRES_PASSWORD=' | cut -d= -f2)
docker rm -f mp-stage-console
docker run -d --name mp-stage-console \
  --restart unless-stopped --network mp-stage-net \
  -p 48200:4730 \
  -v "D:/goai/mp-worktrees/console/evidence:/app/evidence:ro" \
  -e "CONSOLE_PG_DSN=host=mp-stage-pg port=5432 user=mpstage password=$STAGE_PW dbname=mpstage connect_timeout=5" \
  -e "CONSOLE_PILOT_USER=pilot" \
  -e "CONSOLE_PILOT_PASSWORD=pilot-read-only-2026" \
  -e "CONSOLE_SESSION_SECRET=<fresh-random>" \
  -e "CONSOLE_REPO_ALLOWLIST=wookat/speaktype,nghqqa/tizhou" \
  -e "MERGEPILOT_ORG_RAG_A_CHAIN=1" \
  -e "ORG_RAG_LIVE_URL=http://host.docker.internal:48210" \
  mp-canonical-console:candidate
```

## 禁用方式

同上但**不传** `MERGEPILOT_ORG_RAG_A_CHAIN` env（或显式设 0）。
验证：`curl -H "Cookie: ..." ".../api/rag/org-search?q=x"` →
`{"service_state":"a_chain_disabled"}`。

## 健康检查

```bash
# staging console
curl -s http://127.0.0.1:48200/api/health | jq .ok   # → true
# rag-live
curl -s "http://127.0.0.1:48210/api/rag/search?q=ping" | jq .service_state  # → ok
# A 链端到端（登录后）
curl -H "Cookie: $CK" ".../api/rag/org-search?q=密码" | jq .service_state  # → ok
```

## 审计查询

```bash
# 审计日志（JSONL，每行含 snapshot_id/query_hash/source_refs/service_state/run_id）
tail verification/rag-org-knowledge/audit/retrievals.jsonl
# worker 侧审计
tail verification/rag-org-knowledge/audit/worker-retrievals.jsonl
# MinIO 证据桶（如已上传）
docker run --rm --network mp-stage-net --entrypoint sh \
  mp-r13-worker-skill:candidate -c \
  "mc alias set s http://mp-stage-minio:9000 <MU> <MP> && mc ls s/staging-ops/"
```

## 故障处理

| 症状 | 诊断 | 处理 |
|---|---|---|
| `service_state: degraded, reason: service_unreachable` | rag-live 进程退出 | 重启 rag-live（见启用方式步骤 1） |
| `service_state: degraded, reason: corpus_unavailable` | 语料被篡改/被 screening 拒载 | **不修复旧语料**——检查 corpus JSON 是否被改动，从 git 恢复 `verification/rag-org-knowledge/corpus/org-security-standards.v1.json` |
| `service_state: a_chain_disabled` | flag 未设置 | 按启用方式重建容器 |
| 审计 JSONL 丢失 | 磁盘/权限 | 从 MinIO staging-ops 桶恢复 |

## 回滚步骤

```bash
# 1. 禁用 A 链（首选——最轻量）
docker rm -f mp-stage-console
docker run -d --name mp-stage-console ...（不带 MERGEPILOT_ORG_RAG_A_CHAIN）...

# 2. 完整回滚到上一候选镜像
docker rm -f mp-stage-console
docker run -d --name mp-stage-console ... \
  mp-canonical-console:rollback-prev   # sha256:41bd3029

# 3. 核验回滚成功
curl -s http://127.0.0.1:48200/api/health        # → 200
curl -H "Cookie: $CK" .../api/rag/org-search?q=x  # → a_chain_disabled（或 404）
```

## 语义红线

- A 链结果**仅为组织规范参考**（usage_note 字段随每个响应返回）
- 不产生 finding、不改 gate/stage/ticket/success
- degraded 永不显示为正常成功
- 审计缺字段 = 立即停止条件
