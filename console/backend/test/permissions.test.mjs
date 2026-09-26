// console/backend/test/permissions.test.mjs — 权限模型单元 + 会话集成（越权/最小权限）。
// 运行：node --test console/backend/test/permissions.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseAccessModel, resolveRepos, authorize, subjectBranches, denialAudit } from '../lib/permissions.mjs';
import { createConsole } from '../server.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const MODEL = {
  CONSOLE_ACCESS_MODEL_JSON: JSON.stringify([
    { subject: 'alice', kind: 'user', teams: ['eng'], repos: ['acme/app'], branches: ['main'], can_fxv: true, roles: ['viewer'] },
    { subject: 'bob', kind: 'user', teams: [], repos: ['acme/lib'], branches: ['*'] },
    { subject: 'eng', kind: 'team', repos: ['acme/team'], branches: ['*'] },
    { subject: 'root', kind: 'user', teams: [], repos: ['acme/app'], branches: ['*'], can_fxv: true, roles: ['admin'] },
  ]),
};

test('解析：非法 JSON / 非数组 / 非法条目 → invalid（fail-closed）', () => {
  assert.equal(parseAccessModel({ CONSOLE_ACCESS_MODEL_JSON: '{bad' }).mode, 'invalid');
  assert.equal(parseAccessModel({ CONSOLE_ACCESS_MODEL_JSON: '42' }).mode, 'invalid');
  assert.equal(parseAccessModel({ CONSOLE_ACCESS_MODEL_JSON: '[{"subject":"x"}]' }).mode, 'invalid');
  assert.equal(parseAccessModel({}).mode, 'legacy');
});

test('resolveRepos：自身 ∪ 团队', () => {
  const m = parseAccessModel(MODEL);
  assert.deepEqual(resolveRepos(m, 'alice'), ['acme/app', 'acme/team']);
  assert.deepEqual(resolveRepos(m, 'bob'), ['acme/lib']);
  assert.deepEqual(resolveRepos(m, 'ghost'), [], '未知主体=空（最小权限）');
});

test('authorize：未知主体默认拒绝一切', () => {
  const m = parseAccessModel(MODEL);
  assert.equal(authorize(m, 'ghost', { repo: 'acme/app' }).reason, 'SUBJECT_UNKNOWN');
});

test('authorize：跨仓越权拒绝', () => {
  const m = parseAccessModel(MODEL);
  assert.equal(authorize(m, 'bob', { repo: 'acme/app' }).reason, 'REPO_DENIED');
  const d = denialAudit('bob', authorize(m, 'bob', { repo: 'acme/app' }), { repo: 'acme/app' });
  assert.equal(d.kind, 'ACCESS_DENIED');
});

test('authorize：分支越权拒绝', () => {
  const m = parseAccessModel(MODEL);
  assert.equal(authorize(m, 'alice', { repo: 'acme/app', branch: 'dev' }).reason, 'BRANCH_DENIED');
  assert.ok(authorize(m, 'alice', { repo: 'acme/app', branch: 'main' }).ok);
  // 团队仓库用团队分支规则（*）
  assert.ok(authorize(m, 'alice', { repo: 'acme/team', branch: 'any' }).ok);
});

test('authorize：fxv/admin 分级提权拒绝', () => {
  const m = parseAccessModel(MODEL);
  assert.equal(authorize(m, 'alice', { repo: 'acme/app', action: 'fxv' }).ok, true);
  assert.equal(authorize(m, 'bob', { repo: 'acme/lib', action: 'fxv' }).reason, 'FXV_NOT_GRANTED');
  assert.equal(authorize(m, 'alice', { repo: 'acme/app', action: 'admin' }).reason, 'ADMIN_NOT_GRANTED');
  assert.ok(authorize(m, 'root', { repo: 'acme/app', action: 'admin' }).ok);
});

test('会话集成：模型主体=alice 时 session repos=模型解析；越权 repo 403', async () => {
  process.env.CONSOLE_PILOT_USER = 'alice';
  process.env.CONSOLE_PILOT_PASSWORD = 'pw-test';
  process.env.CONSOLE_SESSION_SECRET = 'test-secret';
  delete process.env.CONSOLE_PG_DSN;
  const prev = { ...process.env };
  Object.assign(process.env, MODEL);
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const lr = await fetch(base + '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'alice', password: 'pw-test' }) });
    assert.equal(lr.status, 200);
    const cookie = (lr.headers.getSetCookie?.() || []).map((c) => c.split(';')[0]).join('; ');
    const sr = await (await fetch(base + '/api/auth/session', { headers: { cookie } })).json();
    assert.deepEqual(sr.repos.sort(), ['acme/app', 'acme/team']);
    // 越权仓库（bob 的 acme/lib）→ 403
    const denied = await fetch(base + '/api/pulls?repo=acme/lib', { headers: { cookie } });
    assert.equal(denied.status, 403);
    const db = await denied.json();
    assert.equal(db.error?.reason ?? db.reason, 'repo_not_in_allowlist');
    // 允许仓库 → 200（BACKEND_NOT_WIRED 诚实空）
    const allowed = await fetch(base + '/api/pulls?repo=acme/app', { headers: { cookie } });
    assert.equal(allowed.status, 200);
  } finally {
    server.close();
    for (const k of Object.keys(MODEL)) delete process.env[k];
    Object.assign(process.env, prev);
  }
});

test('会话回归：未配置模型时 legacy allowlist 语义不变', async () => {
  process.env.CONSOLE_PILOT_USER = 'pilot';
  process.env.CONSOLE_PILOT_PASSWORD = 'pw-test';
  process.env.CONSOLE_SESSION_SECRET = 'test-secret';
  process.env.CONSOLE_REPO_ALLOWLIST = 'wookat/speaktype,nghqqa/tizhou';
  delete process.env.CONSOLE_ACCESS_MODEL_JSON;
  delete process.env.CONSOLE_PG_DSN;
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const lr = await fetch(base + '/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'pw-test' }) });
    const cookie = (lr.headers.getSetCookie?.() || []).map((c) => c.split(';')[0]).join('; ');
    const sr = await (await fetch(base + '/api/auth/session', { headers: { cookie } })).json();
    assert.deepEqual(sr.repos, ['wookat/speaktype', 'nghqqa/tizhou']);
  } finally { server.close(); delete process.env.CONSOLE_REPO_ALLOWLIST; }
});

// ── G-07 多凭证（默认拒绝/越权 403/会话吊销/审计）──
test('G-07: alice/bob 各自登录、bob 越权 403、未知用户默认拒绝', async () => {
  process.env.CONSOLE_ACCESS_MODEL_JSON = JSON.stringify([
    { subject: 'alice', kind: 'user', repos: ['acme/app'], branches: ['*'], can_fxv: true },
    { subject: 'bob', kind: 'user', repos: ['acme/lib'], branches: ['*'] },
  ]);
  process.env.CONSOLE_USER_CREDENTIALS_JSON = JSON.stringify({ alice: 'pw-a-123456', bob: 'pw-b-654321' });
  process.env.CONSOLE_PILOT_USER = 'legacy'; process.env.CONSOLE_PILOT_PASSWORD = 'pw-legacy';
  process.env.CONSOLE_SESSION_SECRET = 'test-secret';
  delete process.env.CONSOLE_PG_DSN;
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const login = async (u, p) => (await fetch(base + '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: u, password: p }) }));
  try {
    assert.equal((await login('alice', 'wrong')).status, 401);
    assert.equal((await login('ghost', 'pw-a-123456')).status, 401, '未知主体默认拒绝');
    assert.equal((await login('alice', 'pw-a-123456')).status, 200);
    const ar = await login('alice', 'pw-a-123456');
    const ac = (ar.headers.getSetCookie?.() || []).map((c) => c.split(';')[0]).join('; ');
    const sr = await (await fetch(base + '/api/auth/session', { headers: { cookie: ac } })).json();
    assert.equal(sr.user.name, 'alice'); assert.deepEqual(sr.repos, ['acme/app']);
    const br = await login('bob', 'pw-b-654321');
    assert.equal(br.status, 200);
    const bc = (br.headers.getSetCookie?.() || []).map((c) => c.split(';')[0]).join('; ');
    const denied = await fetch(base + '/api/pulls?repo=acme/app', { headers: { cookie: bc } });
    assert.equal(denied.status, 403, 'bob 越权 alice 仓库被拒');
    const csrf = (br.headers.getSetCookie?.() || []).find((c) => c.startsWith('mp_csrf=')).split(';')[0].split('=')[1];
    assert.equal((await fetch(base + '/api/auth/logout', { method: 'POST', headers: { cookie: bc, 'x-csrf-token': csrf } })).status, 200);
    assert.equal((await fetch(base + '/api/auth/session', { headers: { cookie: bc } })).status, 401, '吊销后会话失效');
    assert.equal((await login('legacy', 'pw-legacy')).status, 401, '多凭证模式下 legacy 单户被拒');
  } finally { server.close();
    delete process.env.CONSOLE_ACCESS_MODEL_JSON; delete process.env.CONSOLE_USER_CREDENTIALS_JSON; }
});
