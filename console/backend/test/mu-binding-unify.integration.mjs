#!/usr/bin/env node
// console/backend/test/mu-binding-unify.integration.mjs — v18 绑定读写来源统一（审计 E-2）
// 集成测试：mu.binding → mu.repository_binding 幂等回填（零丢失）+ 读侧统一切换行为。
// 运行：FXV_PG_TEST_DSN=postgres://... node console/backend/test/mu-binding-unify.integration.mjs
// （优先用 FXV_PG_TEST_DSN 指定的专用一次性 PG；未设置时回退自起一次性 postgres:16-alpine，
//   随机回环端口，跑毕 docker rm -f。全部数据以随机前缀租户隔离，finally 清理自播种行。）
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const { MU_MIGRATIONS, MU_SCHEMA_LATEST,
  MU_FIXTURE_INSTALLATION_BASE, MU_FIXTURE_REPO_BASE,
  muFixtureInstallationIdOf, muFixtureRepoIdOf } = await import('../lib/multiuser/schema.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

// ── PG 接入：FXV_PG_TEST_DSN（专用共享一次性容器）优先，否则自起一次性容器 ──
let CTR = null;
let dsn = process.env.FXV_PG_TEST_DSN || '';
if (!dsn) {
  CTR = `mu-unify-it-${crypto.randomBytes(4).toString('hex')}`;
  const PORT = 17000 + Math.floor(Math.random() * 90);
  execFileSync('docker', ['run', '-d', '--name', CTR,
    '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  dsn = `postgres://postgres:x@127.0.0.1:${PORT}/mu`;
}
const pool = new Pool({ connectionString: dsn });
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { await pool.query('SELECT 1'); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

// 本套件数据隔离前缀（随机——同一共享容器可多轮/并行重跑互不干扰）
const TAG = crypto.randomBytes(4).toString('hex');
const TENANT_A = `unify-a-${TAG}`;
const TENANT_B = `unify-b-${TAG}`;
const NUM = String(100000 + Number.parseInt(TAG.slice(0, 4), 16) % 800000); // 每轮唯一纯数字段

const store = await createMuStore({ pool });

async function count(sql, params) {
  return Number((await pool.query(sql, params)).rows[0].n);
}

try {
  assert.ok(Number(MU_SCHEMA_LATEST) >= 18, 'schema latest >= 18');
  // ── 阶段 0：迁移到最新（fresh 容器=全量应用；已迁移容器=幂等 no-op）──
  await store.initSchema();
  ok('BU0 initSchema 至最新（含 v18）', Number(MU_SCHEMA_LATEST) >= 18
    && (await count(`SELECT count(*)::int n FROM mu.schema_migrations WHERE version=18`)) === 1);

  // ── 阶段 1：播种 v17 世界（mu.binding 多形态行；repository_binding 权威行）──
  const T = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ($1,'UnifyA') RETURNING tenant_id`, [TENANT_A])).rows[0].tenant_id;
  const TB = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ($1,'UnifyB') RETURNING tenant_id`, [TENANT_B])).rows[0].tenant_id;
  const mkRepo = async (tenantId, providerRepoId, owner = 'acme') => (await pool.query(
    `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name, default_branch)
     VALUES ($1,'github',$2,$3,$4,'main') RETURNING repo_id`, [tenantId, providerRepoId, owner, providerRepoId])).rows[0].repo_id;

  // R1 fixture/active（installation_id 非数字 → fixture 域合成安装）
  const R1 = await mkRepo(T, `u1-${TAG}`);
  // R2 state=revoked（→ binding_state=revoked + revoked_at）
  const R2 = await mkRepo(T, `u2-${TAG}`);
  // R3 installation_state=suspended（→ binding_state=suspended）
  const R3 = await mkRepo(T, `u3-${TAG}`);
  // R4 未知 state（fail-visible → 'error' + error_code）
  const R4 = await mkRepo(T, `u4-${TAG}`);
  // R5 真实安装域：数字 installation_id 且 (tenant,installation) 已登记 + 数字 provider_repo_id
  const R5 = await mkRepo(T, `91${NUM}`);
  // R6 权威跳过：同 repo 已有 repository_binding（GHApp 权威行）+ 旧 mu.binding 行
  const R6 = await mkRepo(T, `u6-${TAG}`);
  // TB 同名 repo（跨租户隔离面）
  const R1B = await mkRepo(TB, `u1-${TAG}`, 'other');

  await pool.query(
    `INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
     VALUES ($1,$2,501,'acme-org',99001)`, [Number(`70${NUM}`), T]);

  const seedBinding = async (repoId, fields) => (await pool.query(
    `INSERT INTO mu.binding (tenant_id, repo_id, kind, installation_id, installation_state, state, created_at, updated_at)
     VALUES ($1,$2,$3,$4,$5,$6, now() - interval '1 day', now()) RETURNING binding_id`,
    [T, repoId, fields.kind ?? 'fixture', fields.installationId ?? `fixture-install-${TAG}`,
      fields.installationState ?? 'active', fields.state ?? 'active'])).rows[0].binding_id;
  const B1 = await seedBinding(R1, {});
  const B2 = await seedBinding(R2, { state: 'revoked' });
  const B3 = await seedBinding(R3, { installationState: 'suspended' });
  const B4 = await seedBinding(R4, { state: 'paused' });
  const B5 = await seedBinding(R5, { kind: 'github_app_installation', installationId: `70${NUM}` });
  const B6 = await seedBinding(R6, {});

  // R6 权威 repository_binding 行（真实 GitHub App 域）
  await store.upsertInstallation({ installationId: Number(`71${NUM}`), tenantId: T,
    accountId: 501, accountLogin: 'acme-org', appId: 99001 });
  const R6_AUTH = await store.upsertRepositoryBinding({ tenantId: T, repoId: R6,
    githubRepoId: Number(`92${NUM}`), owner: 'acme', name: `u6-${TAG}`,
    installationId: Number(`71${NUM}`), defaultBranch: 'main' });

  // 回填前快照（mu.binding 逐字节保留断言用）
  const before = (await pool.query(
    `SELECT binding_id, row_to_json(b)::text AS j FROM mu.binding b WHERE binding_id = ANY($1)`,
    [[B1, B2, B3, B4, B5, B6]])).rows;
  const rbBeforeRepos = await count(
    `SELECT count(*)::int n FROM mu.repository_binding WHERE tenant_id=$1`, [T]);

  // ── 阶段 2：触发 v18 回填（删版本行重放=既有库升级路径）──
  await pool.query(`DELETE FROM mu.schema_migrations WHERE version=18`);
  await store.initSchema();
  ok('BU1 v18 重放应用（升级路径）',
    (await count(`SELECT count(*)::int n FROM mu.schema_migrations WHERE version=18`)) === 1);

  const rbRow = async (repoId) => (await pool.query(
    `SELECT rb.*, i.account_type FROM mu.repository_binding rb
       LEFT JOIN mu.github_app_installation i ON i.installation_id = rb.installation_id
      WHERE rb.tenant_id=$1 AND rb.repo_id=$2`, [T, repoId])).rows[0] ?? null;

  const r1 = await rbRow(R1);
  ok('BU2 零丢失回填：fixture/active 行 → repository_binding（binding_state=active）',
    Boolean(r1) && r1.binding_state === 'active', r1);
  ok('BU2b fixture kind → fixture 域表达（合成安装 account_type=fixture + id 保留区间；github_repo_id 同域）',
    r1?.account_type === 'fixture'
      && Number(r1?.installation_id) >= MU_FIXTURE_INSTALLATION_BASE
      && Number(r1?.installation_id) === muFixtureInstallationIdOf(T)
      && Number(r1?.github_repo_id) >= MU_FIXTURE_REPO_BASE
      && Number(r1?.github_repo_id) === muFixtureRepoIdOf(T, R1), r1);
  const r2 = await rbRow(R2);
  ok('BU3 state=revoked → binding_state=revoked + revoked_at 落时',
    r2?.binding_state === 'revoked' && r2?.revoked_at !== null, r2);
  const r3 = await rbRow(R3);
  ok('BU4 installation_state=suspended → binding_state=suspended',
    r3?.binding_state === 'suspended', r3);
  const r4 = await rbRow(R4);
  ok('BU5 未知 state fail-visible → error + v18_unmapped_binding_state（不中断迁移）',
    r4?.binding_state === 'error' && r4?.error_code === 'v18_unmapped_binding_state', r4);
  const r5 = await rbRow(R5);
  ok('BU6 真实安装域映射：installation_id 沿用数字 id（非 fixture 域）+ github_repo_id 沿用 provider_repo_id（保 webhook 关联）',
    Number(r5?.installation_id) === Number(`70${NUM}`) && r5?.account_type === 'User'
      && Number(r5?.github_repo_id) === Number(`91${NUM}`) && r5?.binding_state === 'active', r5);
  const r6 = await rbRow(R6);
  ok('BU7 权威行不被回填覆盖/重复（同 repo 新表已有行 → 跳过，原行原 id）',
    r6?.binding_id === R6_AUTH.binding_id && Number(r6?.github_repo_id) === Number(`92${NUM}`)
      && (await count(`SELECT count(*)::int n FROM mu.repository_binding WHERE tenant_id=$1 AND repo_id=$2`, [T, R6])) === 1,
    { r6: r6?.binding_id, auth: R6_AUTH.binding_id });

  const keptIdentical = (await pool.query(
    `SELECT count(*)::int n FROM mu.binding b
       JOIN (SELECT unnest($1::uuid[]) AS id, unnest($2::text[]) AS j) s ON s.id = b.binding_id
      WHERE row_to_json(b)::text = s.j`,
    [[B1, B2, B3, B4, B5, B6], before.map((b) => b.j)])).rows[0].n;
  const keptPresent = (await pool.query(
    `SELECT count(*)::int n FROM mu.binding WHERE binding_id = ANY($1)`,
    [[B1, B2, B3, B4, B5, B6]])).rows[0].n;
  ok('BU8 mu.binding 逐字段原样保留（老镜像只读兼容；回填零删改）',
    before.length === 6 && Number(keptIdentical) === 6 && Number(keptPresent) === 6,
    { keptIdentical, keptPresent });

  ok('BU9 fixture 合成安装每租户恰一行（幂等派生）',
    (await count(`SELECT count(*)::int n FROM mu.github_app_installation
                   WHERE tenant_id=$1 AND account_type='fixture'`, [T])) === 1);

  // ── 阶段 3：读侧统一行为 ──
  const listA = await store.listRepositories(T);
  const l1 = listA.find((x) => x.repo_id === R1);
  const l6 = listA.find((x) => x.repo_id === R6);
  ok('BU10 仓库列表 LEFT JOIN 新表生效：仅 mu.binding 的仓库现可见绑定（缺陷主修复点）',
    Boolean(l1?.binding_id) && l1?.installation_state === 'active' && l1?.binding_state === 'active', l1);
  ok('BU10b 权威 GHApp 绑定仓库列表可见（既有行为保持）',
    l6?.binding_id === R6_AUTH.binding_id, l6);
  ok('BU10c 列表行含前端契约字段（binding_id/installation_state/binding_state/owner/name/default_branch）',
    ['binding_id', 'installation_state', 'binding_state', 'owner', 'name', 'default_branch', 'pr_count']
      .every((k) => k in (l1 ?? {})));

  const gateR5 = await store.getBindingForRepo(T, R5);
  ok('BU11 repair 门：安装流仓库（repository_binding 行）放行（此前读 mu.binding 恒 403 binding_required）',
    Boolean(gateR5) && Number(gateR5.installation_id) === Number(`70${NUM}`), gateR5);
  ok('BU11b repair 门 fail-closed：revoked 行不放行',
    (await store.getBindingForRepo(T, R2)) === null);
  ok('BU11c repair 门 fail-closed：suspended/error 行不放行',
    (await store.getBindingForRepo(T, R3)) === null && (await store.getBindingForRepo(T, R4)) === null);

  const rev = await store.revokeBinding(T, R1);
  ok('BU12 revoke 生效于新表：binding_state=revoked + revoked_at；门/列表同步失效',
    Boolean(rev) && rev.binding_state === 'revoked'
      && (await store.getBindingForRepo(T, R1)) === null
      && (await store.listRepositories(T)).find((x) => x.repo_id === R1)?.binding_id == null, rev?.binding_state);
  ok('BU12b mu.binding 原行不被触碰（老镜像视图独立；回滚重放亦不复活新表行）',
    (await pool.query(`SELECT state FROM mu.binding WHERE binding_id=$1`, [B1])).rows[0]?.state === 'active');

  // ── 阶段 4：fixture 写路径（ensureBinding → repository_binding）──
  const R7 = await mkRepo(T, `u7-${TAG}`);
  const f1 = await store.ensureBinding({ tenantId: T, repoId: R7, kind: 'fixture',
    installationId: `fixture-install-${TAG}`, grantedScopes: ['pull_requests:read'] });
  const f2 = await store.ensureBinding({ tenantId: T, repoId: R7, kind: 'fixture',
    installationId: `fixture-install-${TAG}`, grantedScopes: ['pull_requests:read'] });
  ok('BU13 演示流写 repository_binding：active + fixture 域安装 + (tenant,repo) 幂等',
    Boolean(f1?.binding_id) && f1.binding_state === 'active'
      && Number(f1.installation_id) === muFixtureInstallationIdOf(T)
      && Number(f1.github_repo_id) === muFixtureRepoIdOf(T, R7)
      && f1.binding_id === f2.binding_id, { f1: f1?.binding_id, f2: f2?.binding_id });
  await store.revokeBinding(T, R7);
  const f3 = await store.ensureBinding({ tenantId: T, repoId: R7, kind: 'fixture' });
  ok('BU13b 撤销后重绑定恢复 active（MS6 语义保持）',
    f3.binding_id === f1.binding_id && f3.binding_state === 'active' && f3.revoked_at === null);
  ok('BU13c 无仓库/跨租户 repo 前置拒绝（同形 null，不走 FK 炸点）',
    (await store.ensureBinding({ tenantId: TB, repoId: R7, kind: 'fixture' })) === null);
  ok('BU13d 列表可见性：演示流绑定立即出现在仓库列表（前端"已绑定仓库"数据源统一）',
    (await store.listRepositories(T)).find((x) => x.repo_id === R7)?.binding_id === f3.binding_id);

  // ── 阶段 5：跨租户隔离 ──
  const listB = await store.listRepositories(TB);
  ok('BU14 跨租户不可见：B 列表零 A 绑定行、B 门解析不出 A repo 绑定',
    listB.every((x) => x.tenant_id === TB && x.binding_id !== (r1?.binding_id ?? 'x'))
      && listB.find((x) => x.repo_id === R1B)?.binding_id == null
      && (await store.getBindingForRepo(TB, R1)) === null,
    { listB: listB.map((x) => x.repo_id) });

  // ── 阶段 6：重放/重启幂等 ──
  const rbCountA = await count(`SELECT count(*)::int n FROM mu.repository_binding WHERE tenant_id=$1`, [T]);
  const mbCountA = await count(`SELECT count(*)::int n FROM mu.binding WHERE tenant_id=$1`, [T]);
  await pool.query(`DELETE FROM mu.schema_migrations WHERE version=18`);
  await store.initSchema(); // v18 DO 块重执行（升级重放）
  await store.initSchema(); // restart 安全（版本行在位，全 no-op）
  ok('BU15 回填重入：repository_binding 零新增行、mu.binding 零增删',
    (await count(`SELECT count(*)::int n FROM mu.repository_binding WHERE tenant_id=$1`, [T])) === rbCountA
      && (await count(`SELECT count(*)::int n FROM mu.binding WHERE tenant_id=$1`, [T])) === mbCountA,
    { rbCountA, mbCountA });
  ok('BU15b 已 revoke 的回填行不被重放复活',
    (await rbRow(R1))?.binding_state === 'revoked');
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  // 清理自播种行（依赖序，逐条参数化；共享容器保持整洁；schema_migrations v18 保留=已迁移态）
  try {
    const ids = (await pool.query(
      `SELECT tenant_id FROM mu.tenant WHERE slug = ANY($1)`, [[TENANT_A, TENANT_B]])).rows.map((r) => r.tenant_id);
    if (ids.length) {
      await pool.query(`DELETE FROM mu.repository_binding WHERE tenant_id = ANY($1)`, [ids]);
      await pool.query(`DELETE FROM mu.github_app_installation WHERE tenant_id = ANY($1)`, [ids]);
      await pool.query(`DELETE FROM mu.binding WHERE tenant_id = ANY($1)`, [ids]);
      await pool.query(`DELETE FROM mu.repository WHERE tenant_id = ANY($1)`, [ids]);
      await pool.query(`DELETE FROM mu.tenant WHERE tenant_id = ANY($1)`, [ids]);
    }
  } catch { /* 清理尽力而为 */ }
  await pool.end().catch(() => {});
  if (CTR) { try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ } }
}
console.log(`\nmu-binding-unify.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
