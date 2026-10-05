#!/usr/bin/env node
// console/backend/test/cchain-rotate-mu.integration.mjs — D-1（rc.12 PR-B）：
// POST /api/cchain/keystore/rotate 的 MU 会话操作面集成测试（自起一次性
// postgres:16-alpine，随机回环端口；跑毕 docker rm -f——绝不触碰常驻栈；
// 身份/凭据全部为合成 fixture，secret 全程随机生成、不入 Git）。
// 断言：
//  * MU platform_admin + CSRF → 200，新 key 落盘；secret 不在响应、不在审计；
//  * maintainer/contributor/reviewer/auditor → 403 action_not_granted（零副作用）；
//  * 无 X-CSRF-Token / CSRF 篡改 → 403 csrf_required；
//  * grace_ms=0 → 旧 key 即刻失效（文件 expires 收紧断言）；新 key sign+verify
//    往返通过（createRunBindingAuth）；nonce 重放拒绝（REPLAYED_NONCE）；
//  * grace_ms>0 → 旧 key 文件 expires 收紧至轮换时刻+grace，逾期后验签 401；
//  * 审计 KEY_ROTATED actor=mu:<login>（MU 身份入审计）。
// legacy admin 分支回归由 cchain-wiring.integration.mjs 既有断言承担（不重复）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createRunBindingAuth } = await import('../lib/cchain/index.mjs');
const { createConsole } = await import('../server.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `cchain-rot-mu-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16900 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
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
process.env.CONSOLE_PILOT_USER = 'dev-pilot';            // bootstrap 映射源
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'mu-rotate-it-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_FIXTURES = '1';
// C 链 keystore：隔离临时目录 + 合成 seed key（轮换真源）
const KEYS = fs.mkdtempSync(path.join(os.tmpdir(), 'cchain-rot-mu-'));
const SEED = { key_id: 'seed-it', secret: crypto.randomBytes(32).toString('hex'),
  created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 86400_000).toISOString(), revoked: false };
fs.writeFileSync(path.join(KEYS, 'seed-it.key.json'), JSON.stringify(SEED));
Object.assign(process.env, {
  MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS,
  MERGEPILOT_MODEL_CACHE_DIR: '',
  MERGEPILOT_PROVIDER_ATTEST_URL: '',
  MERGEPILOT_CCHAIN_ENFORCE: '',
});

const pool = new Pool({ connectionString: dsn });
// fxv.audit_events（rotateKeystore 审计落点；与 cchain-wiring.integration.mjs 同形）
await pool.query('CREATE SCHEMA IF NOT EXISTS fxv');
await pool.query(`CREATE TABLE IF NOT EXISTS fxv.audit_events (
   seq BIGSERIAL PRIMARY KEY, attempt_id TEXT NOT NULL, kind TEXT NOT NULL,
   from_state TEXT, to_state TEXT, actor TEXT NOT NULL, reason TEXT,
   meta JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
await pool.query(`DELETE FROM fxv.audit_events WHERE attempt_id = 'cchain'`);

const { server } = createConsole({ evidenceRoot: os.tmpdir(), distDir: path.join(os.tmpdir(), 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;

// 每次 login 独立 jar：返回的 cookie 与 csrf 来自同一次会话签发（CSRF 双提交配对）
async function muLogin(subject) {
  let jar = {};
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }),
  });
  for (const c of res.headers.getSetCookie?.() ?? []) {
    const [kv] = c.split(';'); const [k, ...v] = kv.split('=');
    jar[k.trim()] = v.join('=');
  }
  const json = await res.json().catch(() => null);
  const cookie = Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');
  return { status: res.status, json, csrf: json?.csrf ?? null, cookie };
}
async function call(p, { method = 'POST', body = null, cookie = null, csrf = null } = {}) {
  const res = await fetch(BASE + p, {
    method,
    headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => null);
  return { status: res.status, json };
}
const keyFiles = () => fs.readdirSync(KEYS).filter((f) => f.endsWith('.key.json')).sort();
const rotateAuditRows = async () =>
  (await pool.query(`SELECT actor, reason, meta::text AS meta FROM fxv.audit_events
     WHERE attempt_id='cchain' AND kind='KEY_ROTATED' ORDER BY seq`)).rows;

const signer = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS });
async function postVerify(keySecret, runId, nonce) {
  const ts = Date.now();
  return fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ run_id: runId, nonce, timestamp: ts,
      signature: signer.sign(keySecret, { run_id: runId, nonce, timestamp: ts }) }) });
}

try {
  // ── 0) MU 生产模式 legacy login 拒（MU-A1 语义前置确认） ──
  const legacy = await fetch(BASE + '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'dev-pilot', password: 'legacy-test-password' }) });
  ok('MU 模式 legacy 共享账号登录被拒（403 legacy_login_disabled_in_multiuser）',
    legacy.status === 403 && (await legacy.json())?.error?.reason === 'legacy_login_disabled_in_multiuser');

  // ── 1) bootstrap platform_admin 会话 ──
  const admin = await muLogin('fixture:dev-pilot');
  ok('bootstrap pilot 登录 → platform_admin（default tenant）',
    admin.status === 200 && admin.json?.role === 'platform_admin', admin.json);

  // ── 2) 授权矩阵：manage_instance 仅 platform_admin ──
  const ROLES = [['mt-it', 'maintainer'], ['ct-it', 'contributor'], ['rv-it', 'reviewer'], ['ad-it', 'auditor']];
  const sess = {};
  for (const [login, role] of ROLES) {
    const added = await call('/api/mu/members', { cookie: admin.cookie, csrf: admin.csrf,
      body: { login, role } });
    if (added.status !== 200) throw new Error(`member add failed: ${login} ${JSON.stringify(added.json)}`);
    sess[login] = await muLogin(`fixture:${login}`);
    if (sess[login].status !== 200) throw new Error(`login failed: ${login}`);
  }
  const filesBefore = keyFiles();
  for (const [login, role] of ROLES) {
    const r = await call('/api/cchain/keystore/rotate', { cookie: sess[login].cookie, csrf: sess[login].csrf });
    ok(`MU ${role} rotate → 403 action_not_granted（零副作用，不泄露矩阵）`,
      r.status === 403 && r.json?.error?.reason === 'action_not_granted' && r.json?.action === 'manage_instance', r.json);
  }
  ok('非授权角色轮换零副作用（keystore 无新文件、零 KEY_ROTATED 审计）',
    JSON.stringify(keyFiles()) === JSON.stringify(filesBefore) && (await rotateAuditRows()).length === 0);

  // ── 3) 无 CSRF → 403 csrf_required（platform_admin 也不例外） ──
  const noCsrf = await call('/api/cchain/keystore/rotate', { cookie: admin.cookie });
  ok('MU platform_admin 无 X-CSRF-Token → 403 csrf_required',
    noCsrf.status === 403 && noCsrf.json?.error?.reason === 'csrf_required', noCsrf.json);

  // ── 4) platform_admin + CSRF → 200：新 key 落盘 + secret 不在响应/审计 + grace=0 即刻收紧 ──
  const r1At = Date.now();
  const r1 = await call('/api/cchain/keystore/rotate', { cookie: admin.cookie, csrf: admin.csrf,
    body: { grace_ms: 0 } });
  ok('MU platform_admin + CSRF rotate → 200 ok', r1.status === 200 && r1.json?.ok === true
    && r1.json?.new_key_id && r1.json?.expired_key_ids?.includes('seed-it'), r1.json);
  const k1 = r1.json?.key_file ? JSON.parse(fs.readFileSync(r1.json.key_file, 'utf8')) : null;
  ok('新 key 文件落盘（key_id 对应、未吊销、带 ~90d expires）',
    k1 && k1.key_id === r1.json.new_key_id && k1.revoked === false
      && new Date(k1.expires_at).getTime() > Date.now() + 80 * 86400_000, k1);
  ok('secret 不在响应体（rotateKeystore 既有纪律保持）',
    k1 && !JSON.stringify(r1.json).includes(k1.secret));
  const rows1 = await rotateAuditRows();
  ok('KEY_ROTATED 审计 actor=mu:<login>（MU 身份入审计）',
    rows1.length === 1 && rows1[0].actor === 'mu:dev-pilot', rows1);
  ok('secret 不在任何审计行（reason/meta 全查）',
    k1 && !rows1.some((rw) => String(rw.reason).includes(k1.secret) || String(rw.meta).includes(k1.secret)));
  const seedNow = JSON.parse(fs.readFileSync(path.join(KEYS, 'seed-it.key.json'), 'utf8'));
  ok('旧 key 文件 expires 收紧（grace_ms=0 → 轮换时刻即刻到期）',
    seedNow.key_id === 'seed-it' && new Date(seedNow.expires_at).getTime() <= r1At + 50,
    { tightened: seedNow.expires_at, r1At });

  // ── 5) 新 key sign+verify 往返 + nonce 防重放 + 旧 key 验签拒绝 ──
  const vr1 = await postVerify(k1.secret, 'mu-rot-it-1', crypto.randomBytes(8).toString('hex'));
  ok('新 key 签名验签往返（createRunBindingAuth sign → /verify 200）', vr1.status === 200);
  const replayNonce = crypto.randomBytes(8).toString('hex');
  const vr2a = await postVerify(k1.secret, 'mu-rot-it-2', replayNonce);
  const vr2b = await postVerify(k1.secret, 'mu-rot-it-2', replayNonce);
  ok('nonce 重放拒绝（首次 200，重放 401 REPLAYED_NONCE）',
    vr2a.status === 200 && vr2b.status === 401
      && (await vr2b.json())?.reason === 'REPLAYED_NONCE');
  const vo1 = await postVerify(SEED.secret, 'mu-rot-it-3', crypto.randomBytes(8).toString('hex'));
  ok('旧 seed key（已收紧过期）验签拒绝 401', vo1.status === 401);

  // ── 6) grace_ms>0：旧 key 收紧至 轮换时刻+grace，逾期后失效 ──
  const t2 = Date.now();
  const r2 = await call('/api/cchain/keystore/rotate', { cookie: admin.cookie, csrf: admin.csrf,
    body: { grace_ms: 400 } });
  ok('第二次轮换 200（expired 含上一任 key）',
    r2.status === 200 && r2.json?.expired_key_ids?.includes(k1.key_id), r2.json);
  const k1Tightened = JSON.parse(fs.readFileSync(r1.json.key_file, 'utf8'));
  const tightAt = new Date(k1Tightened.expires_at).getTime();
  ok('旧 key 文件 expires 收紧至 轮换时刻+grace_ms（±100ms）',
    Math.abs(tightAt - (t2 + 400)) <= 100, { tightAt, expect: t2 + 400 });
  const k2 = JSON.parse(fs.readFileSync(r2.json.key_file, 'utf8'));
  await new Promise((r) => setTimeout(r, 700)); // 越过 grace 窗口
  const vo2 = await postVerify(k1.secret, 'mu-rot-it-4', crypto.randomBytes(8).toString('hex'));
  const vn2 = await postVerify(k2.secret, 'mu-rot-it-5', crypto.randomBytes(8).toString('hex'));
  ok('grace 逾期后旧 key 验签 401；新 key 验签 200', vo2.status === 401 && vn2.status === 200);
  const rowsAll = await rotateAuditRows();
  ok('全部 KEY_ROTATED actor 均为 mu:dev-pilot 且零 secret 泄露',
    rowsAll.every((rw) => rw.actor === 'mu:dev-pilot')
      && !rowsAll.some((rw) => [k1.secret, k2.secret, SEED.secret]
        .some((s) => String(rw.reason).includes(s) || String(rw.meta).includes(s))),
    rowsAll.map((rw) => rw.actor));

  // ── 7) CSRF 篡改 → 403 csrf_required（timing-safe 摘要比对拒绝错值） ──
  const tampered = (admin.csrf ?? '').slice(0, -1) + (String(admin.csrf).endsWith('a') ? 'b' : 'a');
  const badCsrf = await call('/api/cchain/keystore/rotate', { cookie: admin.cookie, csrf: tampered });
  ok('CSRF 值被篡改 → 403 csrf_required（零副作用）',
    badCsrf.status === 403 && badCsrf.json?.error?.reason === 'csrf_required');
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  fs.rmSync(KEYS, { recursive: true, force: true });
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\ncchain-rotate-mu.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
