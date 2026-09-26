// console/backend/test/cchain.test.mjs — C 链接口层测试（真实文件/真实端口失败/真实 HMAC）。
// 运行：node --test console/backend/test/cchain.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { modelCacheStatus, providerAttestConfig, fetchProviderAttestation,
         runBindingAuthStatus, createRunBindingAuth, cchainStatus } from '../lib/cchain/index.mjs';

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'cchain-'));
const sha256 = (b) => crypto.createHash('sha256').update(b).digest('hex');

test('model cache: 未配置 → NOT_CONFIGURED + 阻塞条件', () => {
  const s = modelCacheStatus({});
  assert.equal(s.state, 'NOT_CONFIGURED');
  assert.match(s.blocked_condition, /MERGEPILOT_MODEL_CACHE_DIR/);
});

test('model cache: 真实文件+正确 manifest → READY；篡改 → CORRUPT；缺文件 → CORRUPT', () => {
  const dir = tmp();
  const blob = Buffer.from('fake-model-weights');
  fs.writeFileSync(path.join(dir, 'model.bin'), blob);
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(
    { model_id: 'test-embed-v1', files: [{ name: 'model.bin', sha256: sha256(blob) }] }));
  assert.equal(modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: dir }).state, 'READY');
  fs.writeFileSync(path.join(dir, 'model.bin'), Buffer.from('tampered'));
  const s2 = modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: dir });
  assert.equal(s2.state, 'CORRUPT');
  assert.ok(s2.problems.some((p) => p.includes('digest mismatch')));
  fs.unlinkSync(path.join(dir, 'model.bin'));
  assert.equal(modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: dir }).state, 'CORRUPT');
});

test('attestation: 未配置 → NOT_CONFIGURED；不可达（真实关闭端口）→ UNREACHABLE fail-closed', async () => {
  assert.equal(providerAttestConfig({}).configured, false);
  const s = await fetchProviderAttestation({ MERGEPILOT_PROVIDER_ATTEST_URL: 'http://127.0.0.1:1/attest' });
  assert.equal(s.state, 'UNREACHABLE');
  assert.match(s.blocked_condition, /不可达/);
});

test('attestation: 形状完整+key_id 匹配 → ATTESTED；key_id 不符 → INVALID', async () => {
  const good = async () => ({ status: 200, json: async () => ({ provider: 'p1', model: 'm1', attestation: { sig: 'x' }, key_id: 'k-expected' }) });
  const s = await fetchProviderAttestation(
    { MERGEPILOT_PROVIDER_ATTEST_URL: 'http://x/', MERGEPILOT_PROVIDER_EXPECTED_KEY_ID: 'k-expected' }, good);
  assert.equal(s.state, 'ATTESTED');
  const bad = async () => ({ status: 200, json: async () => ({ provider: 'p1', model: 'm1', attestation: { sig: 'x' }, key_id: 'k-other' }) });
  const s2 = await fetchProviderAttestation(
    { MERGEPILOT_PROVIDER_ATTEST_URL: 'http://x/', MERGEPILOT_PROVIDER_EXPECTED_KEY_ID: 'k-expected' }, bad);
  assert.equal(s2.state, 'INVALID');
  assert.ok(s2.problems.some((p) => p.includes('key_id')));
});

test('run-binding-auth: 未预置 → BLOCKED 语义；正确签名 → ok；错签/重放/过期 → 拒绝', async () => {
  assert.match(runBindingAuthStatus({}).blocked_condition, /KEYSTORE/);
  const dir = tmp();
  const secret = crypto.randomBytes(32).toString('hex');
  fs.writeFileSync(path.join(dir, 'k1.key.json'), JSON.stringify(
    { key_id: 'k1', secret, created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 3600_000).toISOString(), revoked: false }));
  const env = { MERGEPILOT_RUN_BINDING_KEYSTORE: dir };
  assert.equal(runBindingAuthStatus(env).state, 'READY');
  const auth = createRunBindingAuth(env);
  const nonce = crypto.randomBytes(12).toString('hex');
  const ts = Date.now();
  const sig = auth.sign(secret, { run_id: 'run-1', nonce, timestamp: ts });
  assert.deepEqual(auth.verify({ run_id: 'run-1', nonce, timestamp: ts, signature: sig }), { ok: true });
  // 同 nonce 重放 → 拒绝
  assert.equal(auth.verify({ run_id: 'run-1', nonce, timestamp: ts, signature: sig }).reason, 'REPLAYED_NONCE');
  // 错签名
  assert.equal(auth.verify({ run_id: 'run-1', nonce: 'n2', timestamp: Date.now(), signature: 'f'.repeat(64) }).reason, 'BAD_SIGNATURE');
  // 时间倾斜
  assert.equal(auth.verify({ run_id: 'r', nonce: 'n3', timestamp: Date.now() - 600_000, signature: 'a'.repeat(64) }).reason, 'TIMESTAMP_SKEW');
});

test('cchainStatus: 聚合 BLOCKED 且逐项给出具体阻塞条件（不伪造 READY）', async () => {
  const s = await cchainStatus({});
  assert.equal(s.overall, 'BLOCKED');
  assert.equal(s.blocked_conditions.length, 3);
  for (const c of s.components) assert.ok(c.blocked_condition, `${c.component} 缺阻塞条件`);
});
