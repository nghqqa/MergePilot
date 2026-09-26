// cchain-wiring.test.mjs — C 链接线层合同测试（B 轨 feat/core-b-parallel）。
// 真实服务器进程（createConsole + 随机端口）+ 真临时 keystore/模型缓存目录 +
// 真 HMAC 签验；不接 PG 时审计标志必须诚实（audit_written=false + error）。
// 运行：node --test console/backend/test/cchain-wiring.test.mjs
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { createConsole } from '../server.mjs';
import { auditEvent, enforceGate, resetGateCache, resetCchainObserver, resetDenyAuditLimiter } from '../lib/cchain/wiring.mjs';
import { createRunBindingAuth } from '../lib/cchain/index.mjs';
import { runPipeline } from '../lib/fxv/orchestrator.mjs';

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'cchainw-'));
const sha256hex = (b) => crypto.createHash('sha256').update(b).digest('hex');

let KEYS; let CACHE; let BASE; let srv; let jar = {}; let jarAdmin = {}; let jarBob = {};

const FIXTURE_KEY = { key_id: 'k-fixture', secret: crypto.randomBytes(32).toString('hex'),
  created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 86400_000).toISOString(), revoked: false };

function setCookieJar(store, res) {
  for (const c of res.headers.getSetCookie?.() ?? []) {
    const [kv] = c.split(';');
    const [k, ...v] = kv.split('=');
    store[k.trim()] = v.join('=');
  }
}
const cookieHeader = (store) => Object.entries(store).map(([k, v]) => `${k}=${v}`).join('; ');

async function login(store, user, password) {
  const res = await fetch(BASE + '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user, password }) });
  setCookieJar(store, res);
  return res.status;
}

before(async () => {
  KEYS = tmp(); CACHE = tmp();
  fs.writeFileSync(path.join(KEYS, 'k-fixture.key.json'), JSON.stringify(FIXTURE_KEY));
  fs.writeFileSync(path.join(CACHE, 'model.bin'), Buffer.from('fixture-weights'));
  fs.writeFileSync(path.join(CACHE, 'manifest.json'), JSON.stringify(
    { model_id: 'fixture-embed-v1', files: [{ name: 'model.bin', sha256: sha256hex(Buffer.from('fixture-weights')) }] }));
  Object.assign(process.env, {
    CONSOLE_SESSION_SECRET: 'test-secret-cchain-wiring',
    CONSOLE_ACCESS_MODEL_JSON: JSON.stringify([
      { subject: 'alice', kind: 'user', teams: [], repos: ['acme/app'], branches: ['main'], can_fxv: true, roles: ['admin'] },
      { subject: 'bob', kind: 'user', teams: [], repos: ['acme/app'], branches: ['main'], can_fxv: false, roles: ['viewer'] },
    ]),
    CONSOLE_USER_CREDENTIALS_JSON: JSON.stringify({ alice: 'test-password-a', bob: 'test-password-b' }),
    MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS,
    MERGEPILOT_MODEL_CACHE_DIR: CACHE,
    MERGEPILOT_PROVIDER_ATTEST_URL: '', // 未配置 → attestation NOT_CONFIGURED → overall BLOCKED（如实）
    MERGEPILOT_CCHAIN_ENFORCE: '',
  });
  delete process.env.CONSOLE_PG_DSN;
  resetCchainObserver(); resetGateCache(); resetDenyAuditLimiter();
  const { server } = createConsole({ evidenceRoot: tmp() });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  BASE = `http://127.0.0.1:${server.address().port}`;
  srv = server;
});

after(() => { srv?.close(); });

test('status：未认证 401；认证后 200 且三组件真实状态（attestation 未配置 → overall BLOCKED）', async () => {
  assert.equal((await fetch(BASE + '/api/cchain/status')).status, 401);
  assert.equal(await login(jar, 'alice', 'test-password-a'), 200);
  const res = await fetch(BASE + '/api/cchain/status', { headers: { cookie: cookieHeader(jar) } });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.overall, 'BLOCKED');
  const comps = Object.fromEntries(body.components.map((c) => [c.component, c.state]));
  assert.equal(comps.model_cache, 'READY'); // fixture 缓存真实校验通过
  assert.equal(comps.provider_attestation, 'NOT_CONFIGURED');
  assert.equal(comps.run_binding_auth, 'READY'); // fixture keystore 有有效 key
  // NOT_DISTRIBUTED 语义：B 栈 promote2 部署（keystore 空目录）时状态 MISSING + not_distributed=true（见 Phase6 验证）
  assert.ok(body.blocked_conditions.some((c) => c.includes('provider_attestation')));
  assert.equal(body.enforce.flag, false);
  // 无 DSN：状态变化审计必须诚实标注未落库
  assert.equal(body.audit_note.written, false);
  assert.ok(body.audit_note.error);
});

test('metrics：未认证 401；认证后计数器/快照形状（无伪造 READY）', async () => {
  await fetch(BASE + '/api/cchain/status', { headers: { cookie: cookieHeader(jar) } }); // 刷新快照
  assert.equal((await fetch(BASE + '/api/cchain/metrics')).status, 401);
  const res = await fetch(BASE + '/api/cchain/metrics', { headers: { cookie: cookieHeader(jar) } });
  const m = await res.json();
  assert.equal(m.source, 'cchain');
  assert.deepEqual(Object.keys(m.counters).sort(),
    ['key_rotations', 'run_binding_verify_denied', 'run_binding_verify_denied_suppressed', 'run_binding_verify_ok']);
  assert.equal(m.gauges.cchain_model_cache_ready, 1);
  assert.equal(m.gauges.cchain_provider_attested, 0);
  assert.equal(m.gauges.cchain_run_binding_ready, 1);
  assert.equal(m.gauges.cchain_overall_ready, 0);
  assert.equal(m.enforce_flag, false);
});

test('run-binding 验签合同：BAD_REQUEST 400；有效签名 200；同 nonce 重放 401 REPLAYED_NONCE；坏签名 401；时间窗 401', async () => {
  const signer = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS });
  const post = (payload) => fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });

  let r = await post({ run_id: 'run-1' });
  assert.equal(r.status, 400);
  assert.equal((await r.json()).reason, 'BAD_REQUEST');

  const nonce1 = crypto.randomBytes(12).toString('hex');
  const ts = Date.now();
  const sig1 = signer.sign(FIXTURE_KEY.secret, { run_id: 'run-1', nonce: nonce1, timestamp: ts });
  r = await post({ run_id: 'run-1', nonce: nonce1, timestamp: ts, signature: sig1 });
  assert.equal(r.status, 200);
  let body = await r.json();
  assert.equal(body.ok, true);
  assert.equal(body.audit_written, false); // 无 PG → 诚实

  // 同 nonce 线性重放（同签名）→ 拒绝
  r = await post({ run_id: 'run-1', nonce: nonce1, timestamp: ts, signature: sig1 });
  assert.equal(r.status, 401);
  assert.equal((await r.json()).reason, 'REPLAYED_NONCE');

  // 新 nonce + 错误签名 → BAD_SIGNATURE
  r = await post({ run_id: 'run-1', nonce: crypto.randomBytes(12).toString('hex'),
    timestamp: Date.now(), signature: 'f'.repeat(64) });
  assert.equal(r.status, 401);
  assert.equal((await r.json()).reason, 'BAD_SIGNATURE');

  // 时间窗漂移（±5min 之外）→ TIMESTAMP_SKEW
  r = await post({ run_id: 'run-1', nonce: crypto.randomBytes(12).toString('hex'),
    timestamp: Date.now() - 6 * 60_000,
    signature: signer.sign(FIXTURE_KEY.secret, { run_id: 'run-1', nonce: 'x', timestamp: 1 }) });
  assert.equal(r.status, 401);
  assert.equal((await r.json()).reason, 'TIMESTAMP_SKEW');

  // 计数器可观测
  const m = await (await fetch(BASE + '/api/cchain/metrics', { headers: { cookie: cookieHeader(jar) } })).json();
  assert.ok(m.counters.run_binding_verify_ok >= 1);
  assert.ok(m.counters.run_binding_verify_denied >= 3);
});

test('keystore 轮换：无 CSRF 403；非 admin 403（含 denial）；admin 轮换后旧 key 即刻失效、新 key 可用、secret 不回显', async () => {
  const rotate = (store, hdr = {}) => fetch(BASE + '/api/cchain/keystore/rotate', { method: 'POST',
    headers: { cookie: cookieHeader(store), 'content-type': 'application/json', ...hdr }, body: '{}' });

  assert.equal(await login(jarBob, 'bob', 'test-password-b'), 200);
  let r = await rotate(jarAdmin); // 未登录
  assert.equal(r.status, 401);
  r = await rotate(jar); // alice 已登录但无 CSRF
  assert.equal(r.status, 403);
  assert.equal((await r.json()).error.reason, 'csrf_required');
  r = await rotate(jarBob, { 'x-csrf-token': jarBob.mp_csrf }); // bob 非 admin
  assert.equal(r.status, 403);
  assert.equal((await r.json()).error.reason, 'ADMIN_NOT_GRANTED');

  r = await rotate(jar, { 'x-csrf-token': jar.mp_csrf }); // alice admin
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.ok(body.ok);
  assert.ok(body.new_key_id.startsWith('rk-'));
  assert.ok(body.expired_key_ids.includes('k-fixture'));
  assert.ok(!JSON.stringify(body).includes(FIXTURE_KEY.secret)); // secret 绝不回显
  assert.ok(fs.existsSync(body.key_file));

  // 旧 key（grace=0 已过期）→ 拒绝；新 key → 通过
  const signer = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS });
  const post = (payload) => fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
  const ts = Date.now();
  r = await post({ run_id: 'run-2', nonce: 'n-old', timestamp: ts,
    signature: signer.sign(FIXTURE_KEY.secret, { run_id: 'run-2', nonce: 'n-old', timestamp: ts }) });
  assert.equal(r.status, 401);
  const newKey = JSON.parse(fs.readFileSync(path.join(KEYS, `${body.new_key_id}.key.json`), 'utf8'));
  const n2 = crypto.randomBytes(8).toString('hex');
  r = await post({ run_id: 'run-2', nonce: n2, timestamp: ts,
    signature: signer.sign(newKey.secret, { run_id: 'run-2', nonce: n2, timestamp: ts }) });
  assert.equal(r.status, 200);
});

test('run-binding：keystore 无有效 key → 401 RUN_BINDING_AUTH_BLOCKED（fail-closed）', async () => {
  const emptyDir = tmp(); // 有目录无 key
  const prev = process.env.MERGEPILOT_RUN_BINDING_KEYSTORE;
  process.env.MERGEPILOT_RUN_BINDING_KEYSTORE = emptyDir;
  try {
    const r = await fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ run_id: 'r', nonce: 'n', timestamp: Date.now(), signature: 'a'.repeat(64) }) });
    assert.equal(r.status, 401);
    assert.equal((await r.json()).reason, 'RUN_BINDING_AUTH_BLOCKED');
  } finally { process.env.MERGEPILOT_RUN_BINDING_KEYSTORE = prev; }
});

test('enforcement gate：默认 off 放行；enforce=1 且 C 链未 READY → runPipeline 拒绝启动并留审计', async () => {
  function mockStore(state) {
    const a = { attempt_id: 'att-1', ticket_id: 't-1', repo: 'acme/app', branch: 'main',
      base_head_sha: 'a'.repeat(40), patch_digest: 'd'.repeat(64), state };
    const events = [];
    return { events, a,
      async getAttempt() { return { ...a }; },
      async compareAndSetState(_id, from, to) { if (a.state !== from) return null; a.state = to; return { ...a }; },
      async recordEvent(e) { events.push(e); } };
  }
  // off：正常推进 FILED → AWAITING_APPROVAL（人工等待返回）
  const s1 = mockStore('FILED');
  const out1 = await runPipeline(s1, { repoAllowlist: ['acme/app'], branchAllowlist: ['main'] }, {}, 'att-1');
  assert.equal(out1, 'AWAITING_APPROVAL');

  // on（注入隔离 env：attestation 未配置 → BLOCKED）：拒绝启动，无状态转迁
  resetGateCache();
  const s2 = mockStore('FILED');
  const out2 = await runPipeline(s2, { repoAllowlist: ['acme/app'], branchAllowlist: ['main'], cchain_enforce: true, cchain_env: {} }, {}, 'att-1');
  assert.equal(out2.blocked, 'CCHAIN_BLOCKED');
  assert.ok(out2.blocked_conditions.some((c) => c.includes('provider_attestation')));
  assert.equal(s2.a.state, 'FILED'); // 不转迁，留 TTL 诚实到期
  assert.ok(s2.events.some((e) => e.kind === 'CCHAIN_ENFORCED_BLOCK' && e.actor === 'fxv-runner'));

  // on 且注入"provider 死端口"（真实失败态）→ 仍 BLOCKED：attestation 不可达绝不放行
  resetGateCache();
  const attest = { MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS, MERGEPILOT_MODEL_CACHE_DIR: CACHE,
    MERGEPILOT_PROVIDER_ATTEST_URL: 'http://127.0.0.1:1/x' }; // 死端口 → UNREACHABLE → BLOCKED（真实失败，不伪造）
  const s3 = mockStore('FILED');
  const out3 = await runPipeline(s3, { repoAllowlist: ['acme/app'], branchAllowlist: ['main'], cchain_enforce: true, cchain_env: attest }, {}, 'att-1');
  assert.equal(out3.blocked, 'CCHAIN_BLOCKED');
  resetGateCache();
});

test('enforceGate/env 层：enforce 缺省 off；auditEvent 缺 actor 直接抛错（NOT NULL 纪律）', async () => {
  const g = await enforceGate({ MERGEPILOT_CCHAIN_ENFORCE: '' });
  assert.deepEqual(g, { enforced: false, allowed: true });
  await assert.rejects(() => auditEvent(null, { kind: 'X' }), /actor required/);
});

test('模型缓存负向：篡改文件后 status 探测到 CORRUPT（内容寻址 fail-closed）', async () => {
  fs.writeFileSync(path.join(CACHE, 'model.bin'), Buffer.from('tampered!!'));
  const res = await fetch(BASE + '/api/cchain/status', { headers: { cookie: cookieHeader(jar) } });
  const body = await res.json();
  const mc = body.components.find((c) => c.component === 'model_cache');
  assert.equal(mc.state, 'CORRUPT');
  assert.equal(body.overall, 'BLOCKED');
});

// ── 加固波测试（2026-09-27）：M-1 nonce 有界 / M-2 审计封顶 / L-1 时间戳 / L-2 CSRF ──

test('L-1：timestamp 非有限数值 fail-closed（NaN/Infinity/非数字 → 401 INVALID_TIMESTAMP）；缺字段仍 400', async () => {
  const post = (payload) => fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
  // JSON 无法携带 NaN/Infinity（序列化为 null）→ 走缺字段 400 BAD_REQUEST（fail-closed）
  for (const bad of [NaN, Infinity, -Infinity, null]) {
    const r = await post({ run_id: 'run-l1', nonce: crypto.randomBytes(8).toString('hex'),
      timestamp: bad, signature: 'a'.repeat(64) });
    assert.equal(r.status, 400, `timestamp=${String(bad)} 经 JSON 为 null → BAD_REQUEST`);
    assert.equal((await r.json()).reason, 'BAD_REQUEST');
  }
  // 直接到达 verify 层的非数字值（JSON 可携带的字符串/对象）→ 401 INVALID_TIMESTAMP
  for (const bad of ['not-a-number', { v: 1 }]) {
    const r = await post({ run_id: 'run-l1', nonce: crypto.randomBytes(8).toString('hex'),
      timestamp: bad, signature: 'a'.repeat(64) });
    assert.equal(r.status, 401, `timestamp=${JSON.stringify(bad)}`);
    assert.equal((await r.json()).reason, 'INVALID_TIMESTAMP');
  }
  let r = await post({ run_id: 'run-l1', nonce: 'n', timestamp: Date.now() }); // 缺 signature
  assert.equal(r.status, 400);
  r = await post({ run_id: 'run-l1', nonce: 'n', timestamp: Date.now() - 7 * 60_000, signature: 'a'.repeat(64) });
  assert.equal((await r.json()).reason, 'TIMESTAMP_SKEW'); // skew 语义保持
});

test('M-1：nonce TTL 过期自动清理 + 清理后捕获重放被 skew 拒绝（时钟注入，默认 TTL=10min 下限）', async () => {
  const kdir = tmp();
  const key = { key_id: 'k-ttl', secret: crypto.randomBytes(32).toString('hex'),
    created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 3600_000).toISOString(), revoked: false };
  fs.writeFileSync(path.join(kdir, 'k-ttl.key.json'), JSON.stringify(key));
  // 注入时钟：T0 真实时刻起算（签名时间戳用 T0，之后推进 11min > TTL 下限 10min）
  const T0 = Date.now(); let clock = T0;
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: kdir }, { now: () => clock });
  assert.equal(auth.nonceStats().ttl_ms, 10 * 60_000, 'TTL 钳制下限 = 2× skew 窗口');
  const n = 'ttl-nonce-1'; const ts = T0;
  assert.equal(auth.verify({ run_id: 'r', nonce: n, timestamp: ts,
    signature: auth.sign(key.secret, { run_id: 'r', nonce: n, timestamp: ts }) }).ok, true);
  assert.equal(auth.nonceStats().size, 1);
  clock = T0 + 11 * 60_000; // 推进 11min：nonce TTL(10min) 过期 → 自动清理
  assert.equal(auth.nonceStats().size, 0, '过期 nonce 应被清理');
  // 捕获重放（同 nonce+原 timestamp）：TTL ≥ 2×skew 保证 ts 已出窗 → TIMESTAMP_SKEW 先拒绝
  const replay = auth.verify({ run_id: 'r', nonce: n, timestamp: ts,
    signature: auth.sign(key.secret, { run_id: 'r', nonce: n, timestamp: ts }) });
  assert.equal(replay.reason, 'TIMESTAMP_SKEW');
});

test('M-1：容量上限 FIFO 淘汰（CAP=4：第 5 个成功验签后最旧 nonce 被淘汰，占用恒 ≤ CAP）', async () => {
  const kdir = tmp();
  const key = { key_id: 'k-cap', secret: crypto.randomBytes(32).toString('hex'),
    created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 3600_000).toISOString(), revoked: false };
  fs.writeFileSync(path.join(kdir, 'k-cap.key.json'), JSON.stringify(key));
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: kdir, MERGEPILOT_NONCE_CAP: 4 });
  const doOk = (nonce) => { const ts = Date.now();
    return auth.verify({ run_id: 'r', nonce, timestamp: ts,
      signature: auth.sign(key.secret, { run_id: 'r', nonce, timestamp: ts }) }); };
  assert.equal(doOk('cap-n0').ok, true);
  for (let i = 1; i <= 4; i++) assert.equal(doOk('cap-n' + i).ok, true); // 共 5 个成功
  const st = auth.nonceStats();
  assert.equal(st.capacity, 4);
  assert.equal(st.size, 4, '超容量后 FIFO 淘汰，占用=CAP');
  // 已声明的有界权衡：被 FIFO 淘汰的最旧 nonce（cap-n0）在新 timestamp+有效签名下可再用
  const reused = doOk('cap-n0');
  assert.equal(reused.ok, true, 'FIFO 淘汰后的复用（需持有效密钥+新 ts，权衡已在 index.mjs 注释声明）');
  // 未淘汰的 nonce 重放仍拒绝
  const again = doOk('cap-n3');
  assert.equal(again.reason, 'REPLAYED_NONCE');
});

test('M-1：并发语义——20 个并发同 nonce 验签恰好 1 个成功；30 个并发不同 nonce 全部成功', async () => {
  const kdir = tmp();
  const key = { key_id: 'k-conc', secret: crypto.randomBytes(32).toString('hex'),
    created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 3600_000).toISOString(), revoked: false };
  fs.writeFileSync(path.join(kdir, 'k-conc.key.json'), JSON.stringify(key));
  const auth = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: kdir });
  const ts = Date.now(); const n = 'race-nonce';
  const payload = { run_id: 'r', nonce: n, timestamp: ts,
    signature: auth.sign(key.secret, { run_id: 'r', nonce: n, timestamp: ts }) };
  const results = await Promise.all(Array.from({ length: 20 }, () => Promise.resolve(auth.verify(payload))));
  assert.equal(results.filter((r) => r.ok).length, 1, '同 nonce 并发恰好一次成功');
  assert.equal(results.filter((r) => r.reason === 'REPLAYED_NONCE').length, 19);
  const many = await Promise.all(Array.from({ length: 30 }, (_, i) => {
    const nn = 'conc-' + i; const tts = Date.now();
    return Promise.resolve(auth.verify({ run_id: 'r', nonce: nn, timestamp: tts,
      signature: auth.sign(key.secret, { run_id: 'r', nonce: nn, timestamp: tts }) }));
  }));
  assert.equal(many.filter((r) => r.ok).length, 30);
  assert.ok(auth.nonceStats().size <= 8192);
});

test('M-2：同源拒绝审计封顶（cap=5：8 次失败→5 次审计+3 次 suppressed；拒绝语义不变；成功验签不受限）', async () => {
  resetDenyAuditLimiter();
  const prevCap = process.env.MERGEPILOT_VERIFY_AUDIT_DENY_CAP;
  process.env.MERGEPILOT_VERIFY_AUDIT_DENY_CAP = '5';
  try {
    const post = (payload) => fetch(BASE + '/api/cchain/run-bindings/verify', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
    const bodies = [];
    for (let i = 0; i < 8; i++) {
      const r = await post({ run_id: 'run-m2', nonce: 'm2-' + i, timestamp: Date.now(), signature: 'f'.repeat(64) });
      assert.equal(r.status, 401); // 拒绝语义不变
      bodies.push(await r.json());
    }
    assert.equal(bodies.filter((b) => !b.audit_suppressed).length, 5, '前 5 次拒绝正常走审计路径');
    assert.equal(bodies.filter((b) => b.audit_suppressed === true).length, 3, '超限后审计被抑制并如实标注');
    const m = await (await fetch(BASE + '/api/cchain/metrics', { headers: { cookie: cookieHeader(jar) } })).json();
    assert.ok(m.counters.run_binding_verify_denied_suppressed >= 3, 'suppressed 计数可观测');
    // 成功验签不受封顶影响（audit_suppressed 不出现）
    const keysNow = fs.readdirSync(KEYS).filter((f) => f.endsWith('.key.json'))
      .map((f) => JSON.parse(fs.readFileSync(path.join(KEYS, f), 'utf8')))
      .filter((k) => !k.revoked && new Date(k.expires_at) > new Date())
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    const signer = createRunBindingAuth({ MERGEPILOT_RUN_BINDING_KEYSTORE: KEYS });
    const ts = Date.now(); const n = 'm2-ok-' + crypto.randomBytes(4).toString('hex');
    const r = await post({ run_id: 'run-m2', nonce: n, timestamp: ts,
      signature: signer.sign(keysNow.secret, { run_id: 'run-m2', nonce: n, timestamp: ts }) });
    const body = await r.json();
    assert.equal(r.status, 200);
    assert.notEqual(body.audit_suppressed, true, '成功验签不封顶');
  } finally {
    if (prevCap === undefined) delete process.env.MERGEPILOT_VERIFY_AUDIT_DENY_CAP;
    else process.env.MERGEPILOT_VERIFY_AUDIT_DENY_CAP = prevCap;
    resetDenyAuditLimiter();
  }
});

test('L-2：rotate 的 CSRF timing-safe——错误 token 403 / 长度不同 token 403 / 正确 token 200', async () => {
  const rotate = (hdr) => fetch(BASE + '/api/cchain/keystore/rotate', { method: 'POST',
    headers: { cookie: cookieHeader(jar), 'x-csrf-token': hdr }, body: '{}' });
  assert.equal((await rotate('wrong-token-xyz')).status, 403);
  assert.equal((await rotate('short')).status, 403); // 长度不同（safeEqual 先拒绝）
  assert.equal((await rotate(jar.mp_csrf)).status, 200); // 正确 token（timing-safe 相等）
});
