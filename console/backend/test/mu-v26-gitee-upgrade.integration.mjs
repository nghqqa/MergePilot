#!/usr/bin/env node
// console/backend/test/mu-v26-gitee-upgrade.integration.mjs — Wave-Gitee 升级路径专项：
// 既有 v25 库（含存量 oauth_flow 行/ghapp_install 安装流/GitHub 身份/邀请）→ v26 增量迁移
// （provider DEFAULT 'github-oauth' + bind_user_id）→ 存量语义保真 → 重复应用幂等 →
// 迁移后 GitHub 登录链行为不变。模拟既有库方式与 mu-migration-upgrade.integration.mjs 同构。
// 运行：node console/backend/test/mu-v26-gitee-upgrade.integration.mjs
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-v26-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17480 + Math.floor(Math.random() * 40);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_SESSION_SECRET = 'mu-v26-upgrade-secret';
process.env.CONSOLE_PILOT_USER = 'dev-pilot';

const { MU_MIGRATIONS, MU_SCHEMA_LATEST } = await import('../lib/multiuser/schema.mjs');
const V26 = MU_MIGRATIONS.find((m) => m.version === 26);
const LATEST = MU_SCHEMA_LATEST;

async function applyUpTo(maxVer) {
  await pool.query(`CREATE SCHEMA IF NOT EXISTS mu`);
  await pool.query(`CREATE TABLE IF NOT EXISTS mu.schema_migrations (
    version INT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
  for (const m of MU_MIGRATIONS) {
    if (Number(m.version) > maxVer) break;
    const r = await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=$1`, [m.version]);
    if (r.rowCount) continue;
    for (const sql of m.sql) await pool.query(sql);
    await pool.query(`INSERT INTO mu.schema_migrations (version, name) VALUES ($1,$2)`, [m.version, m.name]);
  }
}

try {
  ok('P0 前提：v26 在迁移链中且为最新', Boolean(V26) && Number(LATEST) >= 26, { latest: LATEST });

  // ── 阶段 1：v25 既有库 + 代表性存量数据（rc.24 生产形态） ──
  await applyUpTo(25);
  await pool.query(`INSERT INTO mu.tenant (tenant_id, slug, display_name) VALUES ('11111111-1111-4111-8111-111111111111','drill','升级演练租户')`);
  await pool.query(`INSERT INTO mu.app_user (user_id, login) VALUES ('22222222-2222-4222-8222-222222222222','legacy-gh-user')`);
  await pool.query(`INSERT INTO mu.external_identity (user_id, provider, subject)
    VALUES ('22222222-2222-4222-8222-222222222222','github-oauth','github-oauth:424242')`);
  await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role)
    VALUES ('11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','maintainer')`);
  // 存量 oauth_flow：登录流（无 purpose 显式值=默认）+ 安装流（ghapp_install）+ 已消费流
  await pool.query(`INSERT INTO mu.oauth_flow (state_hash, corr_hash, expires_at)
    VALUES ('hash-login-flow','hash-corr', now() + interval '10 minutes')`);
  await pool.query(`INSERT INTO mu.oauth_flow (state_hash, corr_hash, expires_at)
    VALUES ('hash-ghapp-flow','hash-corr2', now() + interval '10 minutes')`);
  await pool.query(`UPDATE mu.oauth_flow SET purpose='ghapp_install' WHERE state_hash='hash-ghapp-flow'`);
  await pool.query(`INSERT INTO mu.oauth_flow (state_hash, expires_at, consumed_at)
    VALUES ('hash-consumed', now() + interval '10 minutes', now())`);
  await pool.query(`INSERT INTO mu.invitation (tenant_id, role, expected_subject, expected_login, expires_at)
    VALUES ('11111111-1111-4111-8111-111111111111','reviewer','github-oauth:424242','legacy-gh-user', now() + interval '1 hour')`);
  ok('S1 v25 播种完成（flow×3 + 身份/邀请/成员）',
    (await pool.query(`SELECT count(*)::int n FROM mu.oauth_flow`)).rows[0].n === 3);

  // ── 阶段 2：v26 增量迁移 ──
  for (const sql of V26.sql) await pool.query(sql);
  await pool.query(`INSERT INTO mu.schema_migrations (version, name) VALUES ($1,$2)`, [V26.version, V26.name]);

  const cols = await pool.query(`SELECT column_name, column_default, is_nullable
    FROM information_schema.columns WHERE table_schema='mu' AND table_name='oauth_flow'
    AND column_name IN ('provider','bind_user_id') ORDER BY column_name`);
  ok('S2 v26 两列就位（provider NOT NULL DEFAULT github-oauth + bind_user_id 可空）',
    cols.rows.length === 2
      && cols.rows.find((c) => c.column_name === 'provider')?.is_nullable === 'NO'
      && String(cols.rows.find((c) => c.column_name === 'provider')?.column_default ?? '').includes('github-oauth')
      && cols.rows.find((c) => c.column_name === 'bind_user_id')?.is_nullable === 'YES', cols.rows);

  const flows = await pool.query(`SELECT state_hash, provider, purpose, bind_user_id FROM mu.oauth_flow ORDER BY state_hash`);
  ok('S3 存量登录流回填 provider=github-oauth（语义不变）',
    flows.rows.find((f) => f.state_hash === 'hash-login-flow')?.provider === 'github-oauth'
      && flows.rows.find((f) => f.state_hash === 'hash-login-flow')?.purpose === 'oauth_login');
  ok('S4 存量 ghapp_install 流回填 provider=github-oauth（安装流回调只看 purpose——行为不变）',
    flows.rows.find((f) => f.state_hash === 'hash-ghapp-flow')?.provider === 'github-oauth'
      && flows.rows.find((f) => f.state_hash === 'hash-ghapp-flow')?.purpose === 'ghapp_install');
  ok('S5 存量已消费流保持 consumed（不复活）',
    flows.rows.find((f) => f.state_hash === 'hash-consumed')?.provider === 'github-oauth'
      && (await pool.query(`SELECT consumed_at IS NOT NULL c FROM mu.oauth_flow WHERE state_hash='hash-consumed'`)).rows[0].c);

  // ── 阶段 3：v26 SQL 重复应用幂等（IF NOT EXISTS 语义） ──
  let idemErr = null;
  try { for (const sql of V26.sql) await pool.query(sql); } catch (e) { idemErr = e; }
  ok('S6 v26 SQL 重复应用幂等（不报错不重复）', idemErr === null
    && (await pool.query(`SELECT count(*)::int n FROM information_schema.columns
      WHERE table_schema='mu' AND table_name='oauth_flow' AND column_name='provider'`)).rows[0].n === 1);

  // ── 阶段 4：升到最新（≥v26 的后续迁移不受影响）+ 新写路径按新合同工作 ──
  await applyUpTo(LATEST);
  await pool.query(`INSERT INTO mu.oauth_flow (state_hash, expires_at, provider, purpose, bind_user_id)
    VALUES ('hash-bind-flow', now() + interval '10 minutes', 'gitee-oauth', 'bind_gitee',
      '22222222-2222-4222-8222-222222222222')`);
  const bindFlow = await pool.query(`SELECT provider, purpose, bind_user_id FROM mu.oauth_flow WHERE state_hash='hash-bind-flow'`);
  ok('S7 升满后新写路径（gitee 绑定流三字段）正常',
    bindFlow.rows[0]?.provider === 'gitee-oauth' && bindFlow.rows[0]?.purpose === 'bind_gitee'
      && bindFlow.rows[0]?.bind_user_id === '22222222-2222-4222-8222-222222222222');

  // ── 阶段 5：迁移后 GitHub 存量消费链不变（consumeOAuthFlow CAS 对回填行照常工作） ──
  const consumed = await pool.query(`UPDATE mu.oauth_flow SET consumed_at=now()
    WHERE state_hash='hash-login-flow' AND consumed_at IS NULL AND expires_at > now() RETURNING state_hash, provider`);
  ok('S8 回填行 CAS 单次消费照常（GitHub 链零变化）',
    consumed.rows.length === 1 && consumed.rows[0].provider === 'github-oauth');

  // ── 阶段 6：initSchema 全链（fresh DB v1→latest）幂等重放由 mu-migration-upgrade 覆盖；
  //    此处只验证版本行唯一 ──
  const dup = await pool.query(`SELECT count(*)::int n FROM mu.schema_migrations WHERE version=26`);
  ok('S9 版本行 v26 唯一', dup.rows[0].n === 1);
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-v26-gitee-upgrade.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
