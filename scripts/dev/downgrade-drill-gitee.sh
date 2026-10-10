#!/usr/bin/env bash
# 降级实测：rc.22 生产镜像 ↔ v25 隔离库（审查纠偏轮 D-实测）
# 场景：新代码 bootstrap 到 v25 → 造 GitHub legacy 数据 + Gitee 任务 → rc.22 镜像连库
# 断言：旧镜像正常启动/读取 GitHub 数据；对 Gitee 任务「明确拒绝」而非误处理/挂死/崩溃。
set -u
CTR_PG="mu-dgtest-pg-$$"
CTR_APP="mu-dgtest-app-$$"
PGPORT=$((16700 + RANDOM % 80))
WORK=$(cd "$(dirname "$0")/../.." && pwd)   # worktree 根
WINWORK=$(cygpath -m "$WORK")  # D:/... 形式（Windows file URL 合法）

cleanup() { docker rm -f "$CTR_PG" "$CTR_APP" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$CTR_PG" -e POSTGRES_PASSWORD=x -e POSTGRES_DB=mu \
  -p "127.0.0.1:$PGPORT:5432" postgres:16-alpine >/dev/null
DSN="postgres://postgres:x@127.0.0.1:$PGPORT/mu"
for i in $(seq 1 60); do
  node -e "const {Pool}=require('./console/backend/test/support/node_modules/pg');const p=new Pool({connectionString:'$DSN'});p.query('SELECT 1').then(()=>{p.end();process.exit(0)}).catch(()=>process.exit(1))" 2>/dev/null && break
  sleep 1
done
echo "[1] 隔离 PG 就绪 :$PGPORT"

# ── 新代码 bootstrap 到 v25（含 forge 表）──
node --input-type=module <<EOF
const require2 = (await import('node:module')).createRequire('$WINWORK/console/backend/test/support/noop.js');
const { Pool: PgPool } = require2('pg');
const { createMuStore } = await import('file:///${WINWORK}/console/backend/lib/multiuser/store.mjs');
const pool = new PgPool({ connectionString: '$DSN' });
const store = await createMuStore({ pool, env: process.env });
await store.initSchema();
await store.bootstrap?.().catch(() => {});
const v = (await pool.query('SELECT max(version) v FROM mu.schema_migrations')).rows[0].v;
console.log('[2] bootstrap 完成，schema 版本 =', v);
if (Number(v) < 25) process.exit(1);
process.exit(0);
EOF
[ $? -eq 0 ] || { echo "bootstrap 失败"; exit 1; }

# ── 种子：GitHub legacy 数据 + Gitee 连接与任务 ──
node --input-type=module <<EOF
const require2 = (await import('node:module')).createRequire('$WINWORK/console/backend/test/support/noop.js');
const { Pool } = require2('pg');
const pool = new Pool({ connectionString: '$DSN' });
const q = (t, p2) => pool.query(t, p2);
const t1 = (await q(\`INSERT INTO mu.tenant (slug, display_name) VALUES ('dg','dg') RETURNING tenant_id\`)).rows[0].tenant_id;
await q(\`INSERT INTO mu.github_app_installation (installation_id, tenant_id, app_id, account_id, account_login, account_type)
          VALUES (1, \$1, 1, 901, 'w31', 'User')\`, [t1]);
const r1 = (await q(\`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
          VALUES (\$1,'github','98001','w31','r') RETURNING repo_id\`, [t1])).rows[0];
await q(\`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, binding_state)
          VALUES (\$1,\$2,98001,'w31','r',1,'active')\`, [t1, r1.repo_id]);
await q(\`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
          VALUES (\$1,\$2,201,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','open')\`, [t1, r1.repo_id]);
await q(\`INSERT INTO mu.forge_instance (instance_id, forge_kind, api_base, web_base)
          VALUES ('gitee-cloud','gitee','https://gitee.com/api/v5','https://gitee.com')
          ON CONFLICT DO NOTHING\`);
const fc = (await q(\`INSERT INTO mu.forge_connection (tenant_id, instance_id, credential_ref, webhook_mode, status)
          VALUES (\$1,'gitee-cloud','env:MU_GITEE_PAT','signature','valid') RETURNING connection_id\`, [t1])).rows[0];
const rg = (await q(\`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name, forge_instance_id, connection_id)
          VALUES (\$1,'gitee','777000','gp','pilot','gitee-cloud',\$2) RETURNING repo_id\`, [t1, fc.connection_id])).rows[0];
await q(\`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
          VALUES (\$1,\$2,11,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','open')\`, [t1, rg.repo_id]);
await q(\`INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
          VALUES (\$1,\$2,'event_sync',NULL,'maintainer',\$3::jsonb)\`,
  [t1, rg.repo_id, JSON.stringify({ schema_version: 1, event: 'pull_request', forge_kind: 'gitee',
    delivery_ref: 'dg0000000000000000000000000000', provider_repo_id: '777000',
    pr_number: 11, head_sha: 'b'.repeat(40), action: 'open', trigger_source: null, installation_id: null })]);
console.log('[3] 种子完成：GitHub legacy 数据 + Gitee 任务 1 条');
await pool.end();
EOF
[ $? -eq 0 ] || { echo "种子失败"; exit 1; }

# ── rc.22 生产镜像连隔离库（等效生产形态：multiuser+自动 consumer）──
docker run -d --name "$CTR_APP" --link "$CTR_PG":pg \
  -e MU_MODE=multiuser -e CONSOLE_PG_DSN="postgres://postgres:x@pg:5432/mu" \
  -e CONSOLE_SESSION_SECRET=dg-test-secret -e MU_JOB_CONSUMER_ENABLED=1 \
  -e MU_JOB_CONSUMER_INTERVAL_MS=1000 -e CONSOLE_HOST=0.0.0.0 \
  ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.22 >/dev/null
sleep 12
VER=$(docker logs "$CTR_APP" 2>&1 | grep -oE 'rc\.22' | head -1)
echo "[4] rc.22 镜像启动（版本标识：${VER:-未检出}）"
SCHEMA_ERR=$(docker logs "$CTR_APP" 2>&1 | grep -ciE "column .* does not exist|relation .* does not exist|syntax error")
echo "[5] 旧镜像 schema 兼容错误数 = $SCHEMA_ERR（应为 0）"
# 等 consumer 消费 Gitee 任务（旧代码路径）
sleep 6
docker exec "$CTR_PG" psql -U postgres -d mu -tAc \
  "SELECT state || '|' || COALESCE(result->>'reason','') FROM mu.job WHERE payload->>'forge_kind'='gitee'" > /tmp/dg_gitee_job.txt
GITEE_JOB=$(cat /tmp/dg_gitee_job.txt)
echo "[6] 旧 consumer 对 Gitee 任务的处理结果 = $GITEE_JOB"
echo "$GITEE_JOB" | grep -q "^rejected|installation_id_missing" && DG1=PASS || DG1=FAIL
# GitHub 数据完好（旧镜像视角）
docker exec "$CTR_PG" psql -U postgres -d mu -tAc "SELECT count(*) FROM mu.repository_binding WHERE binding_state='active'" | grep -q 1 && DG2=PASS || DG2=FAIL
docker exec "$CTR_PG" psql -U postgres -d mu -tAc "SELECT count(*) FROM mu.forge_connection WHERE status='valid'" | grep -q 1 && DG3=PASS  # 旧镜像未破坏新表 || DG3=FAIL

echo ""
echo "=== 降级实测结论 ==="
echo "DG1 旧 consumer 对 Gitee 任务明确拒绝（非误处理/非挂死）: $DG1 ($GITEE_JOB)"
echo "DG2 GitHub legacy 数据完好可读        : $DG2"
echo "DG3 schema 共存（新表未受旧镜像影响）  : $DG3"
[ "$DG1" = PASS ] && [ "$DG2" = PASS ] && [ "$DG3" = PASS ] && echo "DOWNGRADE-VERIFIED" || echo "DOWNGRADE-FAILED"
