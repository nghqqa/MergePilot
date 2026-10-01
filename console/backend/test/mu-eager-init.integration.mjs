// console/backend/test/mu-eager-init.integration.mjs — Wave 3.9 Beta 硬化：
// schema 初始化从「首个 /api/mu 请求惰性触发」改为启动阶段显式执行（readiness gate）。
// 回归：fresh DB 上 console 一启动即迁移——health 披露 mu_schema_ready，
// 不经任何业务请求 schema 就绪；facade（独立 pool）首查不 relation-not-exist。
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-eager-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16800 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const adminPool = new Pool({ connectionString: dsn });
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { await adminPool.query('SELECT 1'); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'x-test-password';
process.env.CONSOLE_SESSION_SECRET = 'eager-init-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';

const preTables = (await adminPool.query(
  `SELECT count(*)::int c FROM information_schema.tables WHERE table_schema='mu'`)).rows[0].c;

try {
  // 关键顺序：createConsole 后【不经过任何 /api/mu 业务请求】直接看 health/DB
  const { createConsole } = await import('../server.mjs');
  const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const BASE = `http://127.0.0.1:${server.address().port}`;

  // E1：ready 轮询（readiness gate 语义——启动即迁移，最迟数秒就绪）
  let health = null, readySeen = false;
  for (let i = 0; i < 30; i++) {
    const res = await fetch(BASE + '/api/health');
    health = await res.json().catch(() => null);
    if (health?.mu_schema_ready === true) { readySeen = true; break; }
    await new Promise((r) => setTimeout(r, 500));
  }
  ok('E1 启动即迁移：health.mu_schema_ready=true（零业务请求前置）', readySeen, health?.mu_schema_note);
  ok('E2 health 为 200 且带披露字段', health?.ok === true && 'mu_schema_ready' in (health ?? {}));

  const postTables = (await adminPool.query(
    `SELECT count(*)::int c FROM information_schema.tables WHERE table_schema='mu'`)).rows[0].c;
  ok('E3 mu schema 已在库中创建（表数量显著增长）', postTables > preTables + 10, { preTables, postTables });

  // E4：facade 首查（独立 pool 路径）——登录后 /api/overview 立即可用（不 relation-not-exist）
  const loginRes = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }) });
  const cookie = (loginRes.headers.get('set-cookie') || '').split(';')[0];
  const ov = await fetch(BASE + '/api/overview', { headers: { cookie } });
  const ovBody = await ov.json().catch(() => null);
  ok('E4 facade 首查 200 + MU_CANONICAL_LIVE（独立 pool 不再绕过 readiness）',
    ov.status === 200 && ovBody?.data_source === 'MU_CANONICAL_LIVE', { status: ov.status });

  // E5：重复启动（新 server 实例同库）幂等——迁移重放零错误
  server.close();
  await new Promise((r) => setTimeout(r, 300));
  const { createConsole: cc2 } = await import('../server.mjs');
  const s2 = cc2({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => s2.server.listen(0, '127.0.0.1', r));
  let ready2 = false;
  const BASE2 = `http://127.0.0.1:${s2.server.address().port}`;
  for (let i = 0; i < 30; i++) {
    const h2 = await (await fetch(BASE2 + '/api/health')).json().catch(() => null);
    if (h2?.mu_schema_ready === true) { ready2 = true; break; }
    await new Promise((r) => setTimeout(r, 500));
  }
  ok('E5 重复启动/迁移重放幂等（第二次实例同样就绪）', ready2);
  s2.server.close();
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await adminPool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
