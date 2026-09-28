#!/usr/bin/env node
// console/backend/test/mu-store.integration.mjs — MU Phase 1 schema/bootstrap 集成测试。
// 运行：node console/backend/test/mu-store.integration.mjs（自起一次性 postgres:16-alpine 容器，
// 随机回环端口，跑毕 docker rm -f——绝不触碰任何常驻栈；无真实凭据，库口令为 1 字符占位）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 220) : ''}`); }
};

const CTR = `mu-store-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 16000 + Math.floor(Math.random() * 90);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PORT}/mu`;
const deadline = Date.now() + 60_000;
for (;;) {
  try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
  catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
}

const pool = new Pool({ connectionString: dsn });
try {
  const store = await createMuStore({ pool, env: { MU_BOOTSTRAP_ADMIN_LOGIN: 'mu-admin' } });

  // ── MS*：迁移与 bootstrap ──
  ok('MS1 迁移应用且可重复执行（幂等）', (await store.initSchema()) === true && (await store.initSchema()) === true);
  const vers = (await pool.query(`SELECT version FROM mu.schema_migrations ORDER BY version`)).rows.map((r) => r.version);
  ok('MS1b 版本表记录 1+2', vers.includes(1) && vers.includes(2), vers);

  const b1 = await store.bootstrap();
  const b2 = await store.bootstrap();
  ok('MS2 bootstrap 幂等（两次同 tenant/user/membership）',
    b1.tenant.tenant_id === b2.tenant.tenant_id && b1.user.user_id === b2.user.user_id
      && b1.membership.membership_id === b2.membership.membership_id, { t: b1.tenant.slug, u: b1.user.login });
  ok('MS2b 迁移 tenant 标记 + pilot 操作员 → platform_admin',
    b1.tenant.is_migration_tenant === true && b1.tenant.slug === 'default'
      && b1.membership.role === 'platform_admin' && b1.user.login === 'mu-admin');
  ok('MS2c bootstrap 身份映射（fixture provider，无任何 token 列）',
    (await store.getUserByIdentity('fixture', 'fixture:mu-admin'))?.user_id === b1.user.user_id);

  // ── MS*：复合唯一与跨 tenant 隔离 ──
  const tA = await store.ensureTenant({ slug: `ten-a-${Date.now()}`, displayName: 'A' });
  const tB = await store.ensureTenant({ slug: `ten-b-${Date.now()}`, displayName: 'B' });
  const uA = await store.ensureUser({ login: `alice-${Date.now()}` });
  const uB = await store.ensureUser({ login: `bob-${Date.now()}` });
  await store.ensureMembership({ tenantId: tA.tenant_id, userId: uA.user_id, role: 'maintainer' });
  await store.ensureMembership({ tenantId: tB.tenant_id, userId: uB.user_id, role: 'contributor' });

  const SAME_REPO_ID = 'R_gh_1001';
  const rA = await store.ensureRepository({ tenantId: tA.tenant_id, provider: 'github', providerRepoId: SAME_REPO_ID, owner: 'acme', name: 'app' });
  const rB = await store.ensureRepository({ tenantId: tB.tenant_id, provider: 'github', providerRepoId: SAME_REPO_ID, owner: 'acme', name: 'app' });
  ok('MS3 同 provider_repo_id 可存在于两个 tenant（复合唯一含 tenant 维度）',
    rA.repo_id !== rB.repo_id && rA.tenant_id === tA.tenant_id && rB.tenant_id === tB.tenant_id);

  ok('MS4 resolveRepository 按 tenant 收窄：A 的会话解析不出 B 的 repo',
    (await store.resolveRepository(tA.tenant_id, rB.repo_id)) === null
      && (await store.resolveRepository(tB.tenant_id, rA.repo_id)) === null
      && (await store.resolveRepository(tA.tenant_id, rA.repo_id))?.repo_id === rA.repo_id);

  // 同 tenant 同 repo 幂等 upsert（复合唯一冲突走 UPDATE 不抛错）
  const rA2 = await store.ensureRepository({ tenantId: tA.tenant_id, provider: 'github', providerRepoId: SAME_REPO_ID, owner: 'acme', name: 'app' });
  ok('MS5 同 tenant 内同 repo 幂等（同 repo_id）', rA2.repo_id === rA.repo_id);

  // ── MS*：binding 与约束 ──
  const bindA = await store.ensureBinding({ tenantId: tA.tenant_id, repoId: rA.repo_id, kind: 'fixture', installationId: 'inst-synth-a1', grantedScopes: ['pull_requests:read'] });
  const bindA2 = await store.ensureBinding({ tenantId: tA.tenant_id, repoId: rA.repo_id, kind: 'fixture', installationId: 'inst-synth-a1', grantedScopes: ['pull_requests:read'] });
  ok('MS6 binding (tenant,repo,kind) 幂等', bindA.binding_id === bindA2.binding_id);
  await store.revokeBinding(tA.tenant_id, rA.repo_id);
  ok('MS6b 吊销后 getBindingForRepo 为 null（fail-closed）', (await store.getBindingForRepo(tA.tenant_id, rA.repo_id)) === null);
  await store.ensureBinding({ tenantId: tA.tenant_id, repoId: rA.repo_id, kind: 'fixture', installationId: 'inst-synth-a1', grantedScopes: ['pull_requests:read'] });
  ok('MS6c 重启用恢复 active', (await store.getBindingForRepo(tA.tenant_id, rA.repo_id)) !== null);

  let roleCheckRejected = false;
  try { await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,'superadmin')`, [tA.tenant_id, uB.user_id]); }
  catch { roleCheckRejected = true; }
  ok('MS7 角色词汇 CHECK 约束拒绝未知角色', roleCheckRejected);

  let loginDupRejected = false;
  try { await pool.query(`INSERT INTO mu.app_user (login) VALUES ($1)`, [uA.login]); }
  catch { loginDupRejected = true; }
  ok('MS8 login 全局唯一', loginDupRejected);

  // 凭据红线：核心表无任何 token/secret 形状列
  const cols = (await pool.query(
    `SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='mu'`)).rows;
  const secretCols = cols.filter((c) => /token|secret|password|key/i.test(c.column_name));
  ok('MS9 schema 零凭据列（无 token/secret/password/key 形状列）', secretCols.length === 0, secretCols);

  // ── MS*：membership 撤销语义 ──
  await store.revokeMembership(tA.tenant_id, uA.user_id);
  const mRevoked = await store.getMembership(tA.tenant_id, uA.user_id);
  ok('MS10 撤销后 membership.state=revoked（保留历史，authz 按 state fail-closed）', mRevoked?.state === 'revoked');
  await store.ensureMembership({ tenantId: tA.tenant_id, userId: uA.user_id, role: 'reviewer' });
  ok('MS10b 重新授予恢复 active 且角色更新', (await store.getMembership(tA.tenant_id, uA.user_id))?.state === 'active'
    && (await store.getMembership(tA.tenant_id, uA.user_id))?.role === 'reviewer');
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-store.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
