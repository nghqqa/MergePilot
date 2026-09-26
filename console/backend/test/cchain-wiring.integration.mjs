// cchain-wiring.integration.mjs — C 链接线 × 真 PG 审计落库集成测试（B 轨）。
// 运行（需 B 隔离栈 PG）：
//   CCHAIN_TEST_DSN=postgres://... node --test test/cchain-wiring.integration.mjs
// 断言：状态变化审计 / 验签成败审计 / 轮换审计 真实写入 fxv.audit_events，
// 且 actor 全部非空（audit_events.actor NOT NULL 纪律）。
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';

const DSN = process.env.CCHAIN_TEST_DSN;
const supportDir = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), 'support');
const { Client } = createRequire(path.join(supportDir, 'noop.js'))('pg');

let KEYS; let client; let srv; let BASE; let jar = {};
const SEED = { key_id: 'k-it', secret: crypto.randomBytes(32).toString('hex'),
  created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 86400_000).toISOString(), revoked: false };

function setCookieJar(store, res) {
  for (const c of res.headers.getSetCookie?.() ?? []) {
    const [kv] = c.split(';'); const [k, ...v] = kv.split('=');
    store[k.trim()] = v.join('=');
  }
}
const cookieHeader = (store) => Object.entries(store).map(([k, v]) => `${k}=${v}`).join('; ');

before(async () => {
  if (!DSN) throw new Error('CCHAIN_TEST_DSN 未设置（B 隔离栈 PG DSN）');
  client = new Client({ connectionString: DSN });
  await client.connect();
  await client.query('CREATE SCHEMA IF NOT EXISTS fxv');
  await client.query(`CREATE TABLE IF NOT EXISTS fxv.audit_events (
     seq BIGSERIAL PRIMARY KEY, attempt_id TEXT NOT NULL, kind TEXT NOT NULL,
     from_state TEXT, to_state TEXT, actor TEXT NOT NULL, reason TEXT,
     meta JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
  await client.query(`DELETE FROM fxv.audit_events WHERE attempt_id = 'cchain'`);

  KEYS = fs.mkdtempSync(path.join(os.tmpdir(), 'cchainit-'));
  fs.writeFileSync(path.join(KEYS, 'k-it.key.json'), JSON.stringify(SEED));
  Object.assign(process.env, {
    CONSOLE_SESSION_SECRET: 'it-secret',
    CONSOLE_ACCESS_MODEL_JSON: JSON.stringify([
      { subject: 'alice', kind: 'user', teams: [], repos: ['acme/app'], branches: ['main'], can_fxv: true, roles: ['admin'] }]),
    CONSOLE_USER_CREDENTIALS_JSON: JSON.stringify({ alice: 'test-password-it' }),
    MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS,
    MERGEPILOT_MODEL_CACHE_DIR: '',
    MERGEPILOT_PROVIDER_ATTEST_URL: '',
    MERGEPILOT_CCHAIN_ENFORCE: '',
    CONSOLE_PG_DSN: DSN,
  });
  const { createConsole } = await import('../server.mjs');
  const { resetCchainObserver } = await import('../lib/cchain/wiring.mjs');
  resetCchainObserver();
  const { server } = createConsole({ evidenceRoot: fs.mkdtempSync(path.join(os.tmpdir(), 'ev-')) });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  BASE = `http://127.0.0.1:${server.address().port}`;
  srv = server;
});

after(async () => { srv?.close(); await client?.end().catch(() => {}); });

async function auditRows(kind) {
  const r = await client.query(
    'SELECT * FROM fxv.audit_events WHERE attempt_id=$1 AND kind=$2 ORDER BY seq', ['cchain', kind]);
  return r.rows;
}

test('status 首次观测 → CCHAIN_STATE_CHANGED 审计行（actor 非空 + blocked_conditions 入 meta）', async () => {
  const lr = await fetch(BASE + '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'alice', password: 'test-password-it' }) });
  setCookieJar(jar, lr);
  const res = await fetch(BASE + '/api/cchain/status', { headers: { cookie: cookieHeader(jar) } });
  const body = await res.json();
  assert.equal(body.audit_note.written, true);
  const rows = await auditRows('CCHAIN_STATE_CHANGED');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].actor, 'system:cchain');
  assert.ok(rows[0].meta.blocked_conditions.length >= 2); // model MISSING + attest NOT_CONFIGURED
});

test('run-binding 验签成败两行审计（actor=run-binding:<id>，nonce 前缀入 meta）', async () => {
  const { createRunBindingAuth } = await import('../lib/cchain/index.mjs');
  const signer = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS });
  const post = (payload) => fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
  const ts = Date.now(); const n1 = crypto.randomBytes(8).toString('hex');
  let r = await post({ run_id: 'it-run-1', nonce: n1, timestamp: ts,
    signature: signer.sign(SEED.secret, { run_id: 'it-run-1', nonce: n1, timestamp: ts }) });
  assert.equal(r.status, 200);
  assert.equal((await r.json()).audit_written, true);
  r = await post({ run_id: 'it-run-1', nonce: crypto.randomBytes(8).toString('hex'),
    timestamp: ts, signature: '0'.repeat(64) });
  assert.equal(r.status, 401);
  const ok = await auditRows('RUN_BINDING_VERIFY_OK');
  const denied = await auditRows('RUN_BINDING_VERIFY_DENIED');
  assert.equal(ok.length, 1);
  assert.equal(ok[0].actor, 'run-binding:it-run-1');
  assert.equal(denied.at(-1).actor, 'run-binding:it-run-1');
  assert.equal(denied.at(-1).reason, 'BAD_SIGNATURE');
});

test('keystore 轮换审计（actor=操作员）+ 新旧 key 生效切换', async () => {
  const r = await fetch(BASE + '/api/cchain/keystore/rotate', { method: 'POST',
    headers: { cookie: cookieHeader(jar), 'x-csrf-token': jar.mp_csrf, 'content-type': 'application/json' },
    body: '{"grace_ms":0}' });
  assert.equal(r.status, 200);
  const rows = await auditRows('KEY_ROTATED');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].actor, 'alice');
  assert.ok(rows[0].meta.new_key_id);
  // 旧 key 即刻失效（验签拒绝且落审计）
  const { createRunBindingAuth } = await import('../lib/cchain/index.mjs');
  const signer = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS });
  const ts = Date.now(); const n = crypto.randomBytes(8).toString('hex');
  const r2 = await fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ run_id: 'it-run-2', nonce: n, timestamp: ts,
      signature: signer.sign(SEED.secret, { run_id: 'it-run-2', nonce: n, timestamp: ts }) }) });
  assert.equal(r2.status, 401);
});
