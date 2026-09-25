// console/backend/test/core-pilot-api.test.mjs — 核心控制面迁移契约测试。
// 覆盖：会话三件套（契约 v2 形状）、五 Core API 的 401/403/allowlist 过滤、
// BACKEND_NOT_WIRED / BACKEND_ERROR 诚实态、404/400/500 映射、CSRF 强制。
// 运行：node --test console/backend/test/core-pilot-api.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createConsole } from '../server.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

process.env.CONSOLE_PILOT_USER = 'pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'pw-test';
process.env.CONSOLE_SESSION_SECRET = 'test-secret';
process.env.CONSOLE_REPO_ALLOWLIST = 'wookat/speaktype,nghqqa/tizhou';
delete process.env.CONSOLE_PG_DSN;

async function start() {
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { server, base };
}

async function call(base, p, init) {
  const r = await fetch(base + p, init);
  let body = null; try { body = await r.json(); } catch { /* non-json */ }
  return { status: r.status, body, headers: r.headers };
}

test('unauthenticated core APIs → 401 contract shape {error:{reason}}', async () => {
  const { server, base } = await start();
  try {
    for (const p of ['/api/pulls', '/api/pending', '/api/tickets', '/api/evidence', '/api/audit']) {
      const r = await call(base, p);
      assert.strictEqual(r.status, 401, p);
      assert.strictEqual(r.body?.error?.reason, 'not_authenticated', p);
    }
  } finally { server.close(); }
});

test('login lifecycle: bad creds 401 → login 200 → session echo → logout requires CSRF → dead session', async () => {
  const { server, base } = await start();
  try {
    const bad = await call(base, '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'x' }) });
    assert.strictEqual(bad.status, 401);
    const ok = await call(base, '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'pw-test' }) });
    assert.strictEqual(ok.status, 200);
    assert.match(ok.headers.get('set-cookie') || '', /mp_session=[0-9a-f]+\./);
    const cookie = (ok.headers.get('set-cookie') || '').split(';')[0];
    const csrfM = (ok.headers.get('set-cookie') || '').match(/mp_csrf=([0-9a-f]+)/);
    const sess = await call(base, '/api/auth/session', { headers: { cookie } });
    assert.strictEqual(sess.status, 200);
    // R4（FB-06）：user 为结构化对象——前端显示真实用户名，不再"未知用户"
    assert.deepStrictEqual(sess.body?.user, { name: 'pilot' });
    assert.deepStrictEqual(sess.body?.repos, ['wookat/speaktype', 'nghqqa/tizhou']);
    const lo1 = await call(base, '/api/auth/logout', { method: 'POST', headers: { cookie } });
    assert.strictEqual(lo1.status, 403, 'logout without CSRF rejected');
    const lo2 = await call(base, '/api/auth/logout', { method: 'POST',
      headers: { cookie, 'x-csrf-token': csrfM ? csrfM[1] : '' } });
    assert.strictEqual(lo2.status, 200);
    const after = await call(base, '/api/pulls', { headers: { cookie } });
    assert.strictEqual(after.status, 401, 'session dead after logout');
  } finally { server.close(); }
});

test('core APIs authed: BACKEND_NOT_WIRED honest empty + allowlist 403', async () => {
  const { server, base } = await start();
  try {
    const ok = await call(base, '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'pw-test' }) });
    const cookie = (ok.headers.get('set-cookie') || '').split(';')[0];
    const H = { cookie };
    for (const [p, key] of [['/api/pulls', 'pulls'], ['/api/pending', 'pending'],
                            ['/api/tickets', 'tickets'], ['/api/evidence', 'evidence']]) {
      const r = await call(base, p, { headers: H });
      assert.strictEqual(r.status, 200, p);
      assert.deepStrictEqual(r.body.source, 'BACKEND_NOT_WIRED', p);
      assert.deepStrictEqual(r.body[key], [], p + ' empty, no fake data');
    }
    const au = await call(base, '/api/audit', { headers: H });
    assert.strictEqual(au.body.core_source, 'BACKEND_NOT_WIRED');
    const f = await call(base, '/api/pulls?repo=someone/other', { headers: H });
    assert.strictEqual(f.status, 403);
    assert.strictEqual(f.body?.error?.reason, 'repo_not_in_allowlist');
  } finally { server.close(); }
});

test('unknown API → 404; malformed JSON body → 400', async () => {
  const { server, base } = await start();
  try {
    const nf = await call(base, '/api/nope');
    assert.strictEqual(nf.status, 404);
    const bad = await fetch(base + '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: '{oops' });
    assert.strictEqual(bad.status, 400);
  } finally { server.close(); }
});

test('BACKEND_ERROR honest when DSN unreachable', async () => {
  process.env.CONSOLE_PG_DSN = 'postgresql://bad:bad@127.0.0.1:1/bad';
  try {
    const { server, base } = await start();
    const ok = await call(base, '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'pw-test' }) });
    const cookie = (ok.headers.get('set-cookie') || '').split(';')[0];
    const r = await call(base, '/api/pulls', { headers: { cookie } });
    assert.strictEqual(r.body.source, 'BACKEND_ERROR');
    assert.deepStrictEqual(r.body.pulls, []);
    assert.ok(r.body.error, 'error detail present');
    server.close();
  } finally { delete process.env.CONSOLE_PG_DSN; }
});

// ── /api/overview 契约（CANONICAL_CONSOLE_OPERATIONAL_OVERVIEW）────────
test('GET /api/overview — 401 unauth; authed shape with honest stages', async () => {
  const { server, base } = await start();
  try {
    const u = await call(base, '/api/overview');
    assert.strictEqual(u.status, 401);
    const ok = await call(base, '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'pw-test' }) });
    const cookie = (ok.headers.get('set-cookie') || '').split(';')[0];
    const r = await call(base, '/api/overview', { headers: { cookie } });
    assert.strictEqual(r.status, 200);
    const b = r.body;
    for (const k of ['schema_version', 'generated_at', 'source', 'stage_counts',
                     'repository_counts', 'trend', 'pending_summary', 'incidents', 'health']) {
      assert.ok(k in b, 'field ' + k);
    }
    assert.strictEqual(b.schema_version, 1);
    assert.ok(['POSTGRESQL_LIVE', 'BACKEND_NOT_WIRED', 'BACKEND_ERROR'].includes(b.source));
    for (const st of ['REVIEWING', 'ACTION_REQUIRED', 'REMEDIATING', 'VERIFYING', 'PASSED', 'BLOCKED', 'STALE']) {
      assert.ok(st in b.stage_counts, 'stage ' + st);
    }
    // NOT_WIRED（无 DSN）：聚合为空、不虚构
    assert.strictEqual(b.source, 'BACKEND_NOT_WIRED');
    assert.strictEqual(Object.values(b.stage_counts).reduce((a, c) => a + c, 0), 0);
    assert.deepStrictEqual(b.prs, []);
    assert.strictEqual(b.trend.length, 14);
    assert.strictEqual(b.health.postgres, 'NOT_WIRED');
  } finally { server.close(); }
});
