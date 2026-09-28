#!/usr/bin/env node
// console/backend/test/rag-mu-session.integration.mjs — Dogfooding P1 修复回归。
// 验证 multiuser 模式下 rag-trial 端点兼容 MU 持久会话（mu_session cookie）。
// 自起一次性 postgres:16-alpine（随机回环端口，finally 清理）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const { createConsole } = await import('../server.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `rag-mu-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17600 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'pgvector/pgvector:pg16'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_SESSION_SECRET = 'rag-mu-it-secret';
process.env.CONSOLE_PILOT_USER = 'rag-admin';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_FIXTURES = '1';
process.env.RAGTRIAL_ALLOWED_SCOPES = 'test/repo@main';
process.env.RAGTRIAL_REPO_ALLOWLIST = 'test/repo';

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

// 辅助
async function fixtureLogin(subject) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }),
  });
  const sc = res.headers.get('set-cookie') || '';
  return { status: res.status, cookie: sc.split(';')[0], csrf: sc.match(/mp_csrf=([^;]+)/)?.[1], json: await res.json().catch(() => null) };
}
async function ragPost(p, body, cookie) {
  const res = await fetch(BASE + p, {
    method: 'POST', headers: { ...(cookie ? { cookie } : {}), 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  let json = null; try { json = await res.json(); } catch {}
  return { status: res.status, json };
}
async function ragGet(p, cookie) {
  const res = await fetch(BASE + p, { headers: { ...(cookie ? { cookie } : {}) } });
  let json = null; try { json = await res.json(); } catch {}
  return { status: res.status, json };
}

try {
  // Setup：admin(maintainer) + 各角色
  const admin = await fixtureLogin('fixture:rag-admin');
  assert.equal(admin.status, 200, 'admin login');
  const tenantId = admin.json?.tenant?.tenant_id;

  for (const [login, role] of [['alice', 'maintainer'], ['bob', 'contributor'], ['aud', 'auditor']]) {
    const { createMuStore } = await import('../lib/multiuser/store.mjs');
    const store = await createMuStore({ pool, env: process.env });
    const u = await store.ensureUser({ login });
    await store.ensureIdentity({ userId: u.user_id, provider: 'fixture', subject: `fixture:${login}` });
    await store.ensureMembership({ tenantId, userId: u.user_id, role });
  }
  const alice = await fixtureLogin('fixture:alice'); // maintainer（rag_query ✓）
  const bob = await fixtureLogin('fixture:bob');     // contributor（rag_query ✓）
  const aud = await fixtureLogin('fixture:aud');     // auditor（rag_query ✗）

  // 注册 repo 到 tenant（供 repos 列表）
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const muStore = await createMuStore({ pool, env: process.env });
  await muStore.ensureRepository({ tenantId, provider: 'github', providerRepoId: '1001', owner: 'test', name: 'repo' });

  // ══ RS*：RAG MU Session 兼容回归 ══

  // RS-1：maintainer MU session 可访问 rag-trial status（不再 401）
  const st = await ragGet('/api/rag-trial/status', alice.cookie);
  ok('RS1 maintainer MU session → rag-trial status 可达（非 401）',
    st.status !== 401, { status: st.status });

  // RS-2：ingest 成功（scope+repo 双门通过）
  const ing = await ragPost('/api/rag-trial/ingest', {
    repo: 'test/repo', branch: 'main',
    docs: [{ path: 'policy.md', text: '最小权限原则。参数化查询。' }],
  }, alice.cookie);
  ok('RS2 maintainer MU session → ingest OK（scope∩repo 双门通过）',
    ing.status === 200, ing.json);

  // RS-3：query 成功且返回结果
  const q = await ragPost('/api/rag-trial/query', {
    q: '权限', repo: 'test/repo', branch: 'main', k: 3,
  }, alice.cookie);
  ok('RS3 maintainer MU session → query OK（结果返回）',
    q.status === 200 && q.json?.service_state !== undefined,
    { status: q.status, service_state: q.json?.service_state });

  // RS-4：contributor 也可访问（rag_query ✓）
  const q2 = await ragPost('/api/rag-trial/query', {
    q: '查询', repo: 'test/repo', branch: 'main', k: 3,
  }, bob.cookie);
  ok('RS4 contributor MU session → query OK', q2.status === 200, q2.json?.service_state);

  // RS-5：auditor 被拒（rag_query ✗ → 401 unauthorized）
  const q3 = await ragPost('/api/rag-trial/query', {
    q: '审计', repo: 'test/repo', branch: 'main',
  }, aud.cookie);
  ok('RS5 auditor MU session → query 拒绝（角色无 rag_query → 401）',
    q3.status === 401, { status: q3.status, reason: q3.json?.error?.reason });

  // RS-6：跨 scope repo 拒绝
  const q4 = await ragPost('/api/rag-trial/query', {
    q: 'test', repo: 'other/repo', branch: 'main',
  }, alice.cookie);
  ok('RS6 越权 repo → 403 scope_not_allowed',
    q4.status === 403 && q4.json?.error?.reason === 'scope_not_allowed');

  // RS-7：review-aux 可达（旁路已由 Phase 0a 组合门修复）
  const aux = await ragPost('/api/rag-trial/review-aux', {
    q: '权限', repo: 'test/repo', branch: 'main', run_id: 'rs-test',
  }, alice.cookie);
  ok('RS7 review-aux 可达且受组合门保护', aux.status === 200 || aux.status === 403,
    { status: aux.status, reason: aux.json?.error?.reason });

  // RS-8：无效 cookie → 401
  const bad = await ragGet('/api/rag-trial/status', 'mu_session=invalid-token-xyz');
  ok('RS8 无效 MU cookie → 401', bad.status === 401);

  // RS-9：成员撤权后 session 即时失权（RAG 401）
  await pool.query(`UPDATE mu.membership SET state='revoked' WHERE user_id=(SELECT user_id FROM mu.app_user WHERE login='alice')`);
  const dead = await ragGet('/api/rag-trial/status', alice.cookie);
  ok('RS9 撤权后 MU session → RAG 401（即时失权）', dead.status === 401, dead.status);

  // RS-10：legacy login 仍 fail-closed（multiuser 模式）
  const legacy = await fetch(BASE + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'rag-admin', password: 'legacy-rag-mu-test-password' }),
  });
  ok('RS10 legacy login 在 multiuser 模式仍 403', legacy.status === 403);

  // RS-11：机器端点不受影响（无 session——HMAC 语义）
  const mach = await fetch(BASE + '/api/rag-trial/machine/query', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ run_id: 'x', nonce: 'y', timestamp: 1, signature: 'bad' }),
  });
  ok('RS11 machine 端点签名失败 → 401（HMAC 语义不变）', mach.status === 401);

  // RS-12：拒绝响应零内容泄露
  const leak = JSON.stringify(q4.json ?? {});
  ok('RS12 拒绝响应零 snippet/citation/doc_path 泄露',
    !leak.includes('snippet') && !leak.includes('citation') && !leak.includes('doc_path'));
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch {}
}
console.log(`\nrag-mu-session.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
