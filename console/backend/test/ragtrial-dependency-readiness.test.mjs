// console/backend/test/ragtrial-dependency-readiness.test.mjs — 生产依赖就绪 fail-closed 全场景。
// 使用合成 fixture（零真实凭据）；验证 keystore 与 attestation 在 7 种状态下的行为。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import http from 'node:http';
import { createRequire } from 'node:module';

const supportDir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'support');
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
import { fileURLToPath } from 'node:url';
import {
  runBindingAuthStatus, createRunBindingAuth, providerAttestConfig, fetchProviderAttestation,
} from '../lib/cchain/index.mjs';

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'ragdep-'));
const NOW = Date.now();

// ── 合成密钥 fixture 工具（不生成真实凭据）──
function synthKey(dir, { kid = 'rk-synth-test', revoked = false, expired = false, expiresAt = null } = {}) {
  const key = {
    key_id: kid,
    secret: crypto.randomBytes(32).toString('hex'), // 合成——仅用于 fail-closed 验证
    algorithm: 'hmac-sha256-full',
    created_at: new Date(NOW - 86400000).toISOString(),
    expires_at: expiresAt ?? new Date(expired ? NOW - 3600000 : NOW + 30 * 86400000).toISOString(),
    revoked,
    note: 'SYNTHETIC TEST KEY — 绝非生产凭据',
  };
  fs.writeFileSync(path.join(dir, `${kid}.key.json`), JSON.stringify(key));
  return key;
}

// ═══ 1. Secret Manager / Keystore 接入 ═══

test('SM-1 缺配置（env 未设）→ NOT_CONFIGURED/BLOCKED', () => {
  const st = runBindingAuthStatus({});
  assert.equal(st.state, 'NOT_CONFIGURED');
  assert.match(st.blocked_condition, /未设置/);
});

test('SM-2 目录不存在 → MISSING/not_distributed/BLOCKED', () => {
  const st = runBindingAuthStatus({ MERGEPILOT_RUN_BINDING_KEYSTORE: '/nonexistent/path' });
  assert.equal(st.state, 'MISSING');
  assert.equal(st.not_distributed, true);
});

test('SM-3 目录存在但无密钥 → MISSING/not_distributed/BLOCKED', () => {
  const dir = tmp();
  const st = runBindingAuthStatus({ MERGEPILOT_RUN_BINDING_KEYSTORE: dir });
  assert.equal(st.state, 'MISSING');
  assert.equal(st.not_distributed, true);
});

test('SM-4 有效密钥 → READY', () => {
  const dir = tmp();
  synthKey(dir);
  const st = runBindingAuthStatus({ MERGEPILOT_RUN_BINDING_KEYSTORE: dir });
  assert.equal(st.state, 'READY');
  assert.equal(st.key_count, 1);
});

test('SM-5 已撤销密钥 → 验签拒绝', () => {
  const dir = tmp();
  const k = synthKey(dir, { revoked: true });
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: dir });
  const sig = crypto.createHmac('sha256', k.secret).update('run|nonce|123').digest('hex');
  const r = auth.verify({ run_id: 'run', nonce: 'nonce', timestamp: '123', signature: sig });
  assert.equal(r.ok, false);
  // 撤销的密钥被 loadKeys 过滤 → 等效于无密钥 → RUN_BINDING_AUTH_BLOCKED
});

test('SM-6 已过期密钥 → 验签拒绝（等效无密钥）', () => {
  const dir = tmp();
  const k = synthKey(dir, { expired: true });
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: dir });
  const sig = crypto.createHmac('sha256', k.secret).update('run|nonce|123').digest('hex');
  const r = auth.verify({ run_id: 'run', nonce: 'nonce', timestamp: '123', signature: sig });
  assert.equal(r.ok, false);
});

test('SM-7 轮换场景：旧密钥撤销+新密钥生效', () => {
  const dir = tmp();
  const old = synthKey(dir, { kid: 'rk-old', revoked: true });
  const fresh = synthKey(dir, { kid: 'rk-new' });
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: dir });
  const oldSig = crypto.createHmac('sha256', old.secret).update('run|n|123').digest('hex');
  assert.equal(auth.verify({ run_id: 'run', nonce: 'n', timestamp: '123', signature: oldSig }).ok, false);
  const newSig = crypto.createHmac('sha256', fresh.secret).update('run2|n2|456').digest('hex');
  const ts = Date.now();
  const newSig2 = crypto.createHmac('sha256', fresh.secret).update(`run2|n2|${ts}`).digest('hex');
  assert.equal(auth.verify({ run_id: 'run2', nonce: 'n2', timestamp: ts, signature: newSig2 }).ok, true);
});

test('SM-8 错误签名 → BAD_SIGNATURE', () => {
  const dir = tmp();
  synthKey(dir);
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: dir });
  const r = auth.verify({ run_id: 'r', nonce: 'n', timestamp: Date.now(), signature: 'f'.repeat(64) });
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'BAD_SIGNATURE');
});

test('SM-9 nonce 重放 → REPLAYED_NONCE', () => {
  const dir = tmp();
  const k = synthKey(dir);
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: dir });
  const ts = Date.now();
  const sig = crypto.createHmac('sha256', k.secret).update(`r|nonce1|${ts}`).digest('hex');
  assert.equal(auth.verify({ run_id: 'r', nonce: 'nonce1', timestamp: ts, signature: sig }).ok, true);
  assert.equal(auth.verify({ run_id: 'r', nonce: 'nonce1', timestamp: ts, signature: sig }).reason, 'REPLAYED_NONCE');
});

// ═══ 2. External Attestation 接入 ═══

test('EA-1 缺配置 → NOT_CONFIGURED/BLOCKED', async () => {
  const r = await fetchProviderAttestation({});
  assert.equal(r.state, 'NOT_CONFIGURED');
  assert.match(r.blocked_condition, /未设置/);
});

test('EA-2 有效 attestation → ATTESTED', async () => {
  const srv = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ provider: 'bge-m3', model: 'bge-m3', attestation: { digest: 'a'.repeat(64) }, key_id: 'expected-key' }));
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const port = srv.address().port;
  const r = await fetchProviderAttestation({
    MERGEPILOT_PROVIDER_ATTEST_URL: `http://127.0.0.1:${port}/attest`,
    MERGEPILOT_PROVIDER_EXPECTED_KEY_ID: 'expected-key',
  });
  srv.close();
  assert.equal(r.state, 'ATTESTED');
  assert.equal(r.key_id, 'expected-key');
});

test('EA-3 端点不可达 → UNREACHABLE/fail-closed', async () => {
  const r = await fetchProviderAttestation({
    MERGEPILOT_PROVIDER_ATTEST_URL: 'http://127.0.0.1:1/attest', // 端口 1 = 不可达
    MERGEPILOT_PROVIDER_TIMEOUT_MS: '2000',
  });
  assert.equal(r.state, 'UNREACHABLE');
  assert.ok(r.blocked_condition);
});

test('EA-4 key_id 不匹配 → INVALID', async () => {
  const srv = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ provider: 'x', model: 'y', attestation: {}, key_id: 'wrong-key' }));
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const r = await fetchProviderAttestation({
    MERGEPILOT_PROVIDER_ATTEST_URL: `http://127.0.0.1:${srv.address().port}`,
    MERGEPILOT_PROVIDER_EXPECTED_KEY_ID: 'expected-key',
  });
  srv.close();
  assert.equal(r.state, 'INVALID');
  assert.ok(r.problems.some((p) => p.includes('key_id')));
});

test('EA-5 响应缺字段 → INVALID', async () => {
  const srv = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ foo: 'bar' })); // 缺 provider/model/attestation
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const r = await fetchProviderAttestation({
    MERGEPILOT_PROVIDER_ATTEST_URL: `http://127.0.0.1:${srv.address().port}`,
  });
  srv.close();
  assert.equal(r.state, 'INVALID');
  assert.ok(r.problems.some((p) => p.includes('missing')));
});

test('EA-6 非 JSON 响应 → INVALID', async () => {
  const srv = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end('<html>not json</html>');
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const r = await fetchProviderAttestation({
    MERGEPILOT_PROVIDER_ATTEST_URL: `http://127.0.0.1:${srv.address().port}`,
  });
  srv.close();
  assert.equal(r.state, 'INVALID');
});

test('EA-7 HTTP 非 200 → UNREACHABLE', async () => {
  const srv = http.createServer((req, res) => { res.writeHead(503); res.end(); });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const r = await fetchProviderAttestation({
    MERGEPILOT_PROVIDER_ATTEST_URL: `http://127.0.0.1:${srv.address().port}`,
  });
  srv.close();
  assert.equal(r.state, 'UNREACHABLE');
  assert.equal(r.http_status, 503);
});

test('EA-8 超时 → UNREACHABLE（AbortSignal.timeout）', async () => {
  const srv = http.createServer((req, res) => { setTimeout(() => { res.writeHead(200); res.end('{}'); }, 10000); });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const r = await fetchProviderAttestation({
    MERGEPILOT_PROVIDER_ATTEST_URL: `http://127.0.0.1:${srv.address().port}`,
    MERGEPILOT_PROVIDER_TIMEOUT_MS: '500',
  });
  srv.close();
  assert.equal(r.state, 'UNREACHABLE');
});
