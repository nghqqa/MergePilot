// cchain-model-cache.integration.mjs — D-2（rc.12）回归：>2GB 权重文件流式 sha256。
// 缺陷：modelCacheStatus 曾用 fs.readFileSync 整读做 sha256——Node Buffer 2GiB 硬上限，
// 生产 bge-m3 pytorch_model.bin=2,271,145,830B（≈2.12GiB）抛
// "File size (2271145830) is greater than 2 GiB" → /api/cchain/status HTTP 500 →
// model_cache 对生产真实模型结构性不可能 READY。
// 本文件在旧实现（readFileSync）下必红、流式实现下全绿。权重文件用
// open+ftruncate 生成 2.1GiB 稀疏文件（不写数据：ext4/xfs 磁盘占用≈0；NTFS 仅
// 预分配不写入），读取/哈希走真实流式路径。
// 运行：node --test console/backend/test/cchain-model-cache.integration.mjs
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { modelCacheStatus } from '../lib/cchain/index.mjs';
import { createConsole } from '../server.mjs';

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'cchainmc-'));
const sha256 = (b) => crypto.createHash('sha256').update(b).digest('hex');
// 精确越过 2GiB（2,147,483,648B）的测试大小：2.1GiB = 2,254,857,830B
const BIG = Math.floor(2.1 * 1024 ** 3);

function makeSparse(dir, name, size) {
  const fd = fs.openSync(path.join(dir, name), 'w');
  try { fs.ftruncateSync(fd, size); } finally { fs.closeSync(fd); }
}

// 期望摘要的独立计算：truncate 稀疏文件内容为全零字节（POSIX/Windows 语义一致），
// 直接对同等长度的零块增量 sha256——不经文件系统、不经 createReadStream，
// 与被测实现完全独立，构成摘要正确性的交叉验证。
function sha256ZerosIndependent(size) {
  const h = crypto.createHash('sha256');
  const chunk = Buffer.alloc(64 * 1024 * 1024); // 零块复用，内存 O(1)
  let left = size;
  while (left > 0) {
    const n = Math.min(chunk.length, left);
    h.update(n === chunk.length ? chunk : chunk.subarray(0, n));
    left -= n;
  }
  return h.digest('hex');
}

// 探测某文件当前是否真的读不动（权限拒绝是否成立——root/平台语义差异时用于 skip）
function readRefused(p) {
  return new Promise((resolve) => {
    const s = fs.createReadStream(p);
    s.on('error', (e) => resolve({ refused: true, code: e?.code }));
    s.on('ready', () => { s.destroy(); resolve({ refused: false }); });
  });
}

let BIGDIR; // >2GB 稀疏权重缓存目录（直调 + status 端点共用，避免重复建 2GB 文件）

before(async () => {
  BIGDIR = tmp();
  makeSparse(BIGDIR, 'pytorch_model.bin', BIG);
  fs.writeFileSync(path.join(BIGDIR, 'manifest.json'), JSON.stringify({
    model_id: 'bge-m3-large-v1',
    files: [{ name: 'pytorch_model.bin', sha256: sha256ZerosIndependent(BIG) }],
  }));
});

after(() => {
  for (const d of [BIGDIR]) { try { fs.rmSync(d, { recursive: true, force: true }); } catch { /* best-effort */ } }
});

// ── 1) 核心缺陷回归：>2GB 文件 READY + 摘要正确 + 哈希稳定 + 耗时可接受 ──

test('D-2 核心：>2GB 稀疏权重 → READY（流式 sha256）；摘要与独立零块计算一致；两次调用哈希稳定；耗时可接受', async () => {
  const t0 = Date.now();
  const s1 = await modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: BIGDIR });
  const ms1 = Date.now() - t0;
  // 旧实现此处抛 "greater than 2 GiB"；修复后必须 READY（失败时只打状态形状，不含内容）
  assert.equal(s1.state, 'READY', `>2GB 权重必须 READY，实得：${JSON.stringify(s1)}`);
  assert.equal(s1.model_id, 'bge-m3-large-v1');
  assert.equal(s1.files, 1);
  const t1 = Date.now();
  const s2 = await modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: BIGDIR });
  const ms2 = Date.now() - t1;
  assert.equal(s2.state, 'READY', '同内容两次流式校验必须同结果（哈希稳定）');
  // 期望摘要来自独立零块计算（非被测代码产物）——能 READY 即逐字节一致
  assert.ok(ms1 < 60_000, `2.1GiB 流式哈希耗时可接受（实得 ${ms1}ms）`);
  assert.ok(ms2 < 60_000, `2.1GiB 流式哈希耗时可接受（实得 ${ms2}ms）`);
  console.log(`[D-2 实测] 2.1GiB(${BIG}B) 稀疏权重：流式 sha256 耗时 ${ms1}ms / ${ms2}ms（两次调用）`);
});

// ── 2) 多文件 manifest 逐项校验 ─────────────────────────────────

test('D-2 语义保持：4 文件 manifest 逐项校验 → READY；篡改单文件 1 字节 → CORRUPT 指名该文件且不泄露内容', async () => {
  const dir = tmp();
  const names = ['config.json', 'tokenizer.json', 'sentencepiece.bpe.model', 'weights.bin'];
  const blobs = Object.fromEntries(names.map((n) => [n, Buffer.from(`payload-of-${n}`)]));
  for (const n of names) fs.writeFileSync(path.join(dir, n), blobs[n]);
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify({
    model_id: 'multi-file-v1',
    files: names.map((n) => ({ name: n, sha256: sha256(blobs[n]) })),
  }));
  const s = await modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: dir });
  assert.equal(s.state, 'READY');
  assert.equal(s.files, 4);

  // 篡改 weights.bin 恰 1 字节（独特标记字节），其余 3 文件不动 → 只报该文件
  const fp = path.join(dir, 'weights.bin');
  const raw = fs.readFileSync(fp);
  raw[0] ^= 0xff;
  fs.writeFileSync(fp, raw);
  const s2 = await modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: dir });
  assert.equal(s2.state, 'CORRUPT');
  assert.deepEqual(s2.problems, ['weights.bin: digest mismatch']);
  // 不把模型内容写进状态/响应：篡改后的字节模式不得出现在结果 JSON
  assert.ok(!JSON.stringify(s2).includes('payload-of-weights.bin'), '状态对象不得包含文件内容');
});

// ── 3) 缺文件 ───────────────────────────────────────────────────

test('D-2 语义保持：manifest 列出但文件被删 → CORRUPT(missing) fail-closed', async () => {
  const dir = tmp();
  const blob = Buffer.from('will-be-deleted');
  fs.writeFileSync(path.join(dir, 'model.bin'), blob);
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(
    { model_id: 'm', files: [{ name: 'model.bin', sha256: sha256(blob) }] }));
  fs.unlinkSync(path.join(dir, 'model.bin'));
  const s = await modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: dir });
  assert.equal(s.state, 'CORRUPT');
  assert.deepEqual(s.problems, ['model.bin: missing']);
});

// ── 4) 权限拒绝（EACCES/EPERM）→ CORRUPT 不 crash ────────────────
// Windows chmod 语义差异：chmod 0o000 仅置只读属性，读不拒绝——无法构造 EACCES。
// 本机实测（win32）：对文件 icacls /deny "<user>:(R)" 后 createReadStream 报 EPERM
// （error 事件），故 win32 走 icacls 拒绝 ACE 构造真实读拒绝；POSIX 走 chmod 0o000。
// 特权用户（root）下 chmod 0o000 不阻断读 / icacls 不可用时 → 如实 skip 并标注。

test('D-2 权限拒绝：读流 EACCES/EPERM → CORRUPT(permission denied)，不 crash', async (t) => {
  const dir = tmp();
  let restore = null; // ACL 拒绝 ACE 恢复（win32）；单一清理钩子保证先恢复再删目录
  t.after(() => {
    if (restore) restore();
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best-effort */ }
  });
  const blob = Buffer.from('perm-model-weights');
  const fp = path.join(dir, 'model.bin');
  fs.writeFileSync(fp, blob);
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(
    { model_id: 'm', files: [{ name: 'model.bin', sha256: sha256(blob) }] }));

  if (process.platform === 'win32') {
    const user = process.env.USERNAME || process.env.USER;
    try {
      execFileSync('icacls', [fp, '/deny', `${user}:(R)`], { stdio: 'pipe' });
      restore = () => { try { execFileSync('icacls', [fp, '/remove:d', user], { stdio: 'pipe' }); } catch { /* best-effort */ } };
    } catch {
      return t.skip('Windows 且 icacls 不可用——无法构造读拒绝；该分支由 Linux CI（chmod 0o000）覆盖');
    }
  } else {
    fs.chmodSync(fp, 0o000);
  }

  // 先确认拒绝确实成立（root/特权语义下可能仍可读——如实 skip，不谎报）
  const probe = await readRefused(fp);
  if (!probe.refused) {
    return t.skip(`当前环境无法构造读拒绝（platform=${process.platform}，特权用户？probe.code=${probe.code ?? 'n/a'}）——需非特权 Linux CI 证据`);
  }

  const s = await modelCacheStatus({ MERGEPILOT_MODEL_CACHE_DIR: dir });
  assert.equal(s.state, 'CORRUPT', '权限拒绝必须 fail-closed 记 CORRUPT（不 crash、不 500）');
  assert.deepEqual(s.problems, ['model.bin: permission denied']);
  assert.ok(!JSON.stringify(s).includes('perm-model-weights'), '状态对象不得包含文件内容');
});

// ── 5) status 端点：>2GB 权重不再 500 ────────────────────────────

test('D-2 端点：/api/cchain/status 对 >2GB 权重返回 200 且 model_cache=READY（旧实现 500）', async (t) => {
  Object.assign(process.env, {
    CONSOLE_SESSION_SECRET: 'test-secret-model-cache',
    CONSOLE_ACCESS_MODEL_JSON: JSON.stringify([
      { subject: 'alice', kind: 'user', teams: [], repos: ['acme/app'], branches: ['main'], can_fxv: true, roles: ['admin'] },
    ]),
    CONSOLE_USER_CREDENTIALS_JSON: JSON.stringify({ alice: 'test-password-a' }),
    MERGEPILOT_MODEL_CACHE_DIR: BIGDIR, // >2GB 稀疏权重 + 正确 manifest
    MERGEPILOT_PROVIDER_ATTEST_URL: '',
    MERGEPILOT_RUN_BINDING_KEYSTORE: '',
    MERGEPILOT_CCHAIN_ENFORCE: '',
  });
  delete process.env.CONSOLE_PG_DSN;
  const { server } = createConsole({ evidenceRoot: tmp() });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const BASE = `http://127.0.0.1:${server.address().port}`;
  t.after(() => server.close());

  const login = await fetch(BASE + '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'alice', password: 'test-password-a' }) });
  assert.equal(login.status, 200);
  const jar = {};
  for (const c of login.headers.getSetCookie?.() ?? []) {
    const [kv] = c.split(';');
    const [k, ...v] = kv.split('=');
    jar[k.trim()] = v.join('=');
  }
  const cookie = Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');

  const t0 = Date.now();
  const res = await fetch(BASE + '/api/cchain/status', { headers: { cookie } });
  const ms = Date.now() - t0;
  assert.equal(res.status, 200, `>2GB 权重下 status 端点必须 200（旧 readFileSync 实现此处 500，实得 ${res.status}）`);
  const body = await res.json();
  const mc = body.components.find((c) => c.component === 'model_cache');
  assert.equal(mc.state, 'READY');
  assert.equal(mc.model_id, 'bge-m3-large-v1');
  assert.equal(mc.files, 1);
  console.log(`[D-2 实测] /api/cchain/status（>2GB 权重）HTTP 200，端到端 ${ms}ms`);
  // 其余组件如实 BLOCKED（attestation/keystore 未配置）——本测试只断言 model_cache 不再 500
  assert.equal(body.overall, 'BLOCKED');
  assert.ok(body.blocked_conditions.some((c) => c.startsWith('provider_attestation')));
});
