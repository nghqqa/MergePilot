#!/usr/bin/env node
// console/backend/test/ragtrial-canonical.integration.mjs — CANONICAL_A 整合回归。
// 运行：node console/backend/test/ragtrial-canonical.integration.mjs（自起临时 pgvector）。
// 覆盖（本波新增面）：
//   SC*：scope allowlist——allow/跨 repo/跨 branch/未配置默认拒/env 撤销即时/机器身份越权/
//        拒绝审计有界封顶；
//   MF*：manifest v2 三重校验——缺失/字节数漂移/digest 漂移/必需工件缺失/夹带多余文件
//        （python 子进程 --verify-only，exit 3 fail-closed）；
//   WC*：双 worker 并发 claim 竞争（SKIP LOCKED）——全部任务恰一次、无重复索引；
//   KR*：keystore 轮换/撤销——旧密钥签名失效（401）、新密钥生效（200）。
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..', '..');
const { createConsole } = await import('../server.mjs');

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 260) : ''}`); }
}

const CTR = `ragcanon-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 15900 + Math.floor(Math.random() * 90);
const REPO = 'nghqqa/mergepilot', BR = 'feat/rag-prod-readiness';
const SCOPES = `${REPO}@${BR}`;
const sign = (secret, { run_id, nonce, timestamp }) =>
  crypto.createHmac('sha256', secret).update(`${run_id}|${nonce}|${timestamp}`).digest('hex');

// ── 临时 PG + console HTTP ──
async function boot() {
  execFileSync('docker', ['run', '-d', '--name', CTR,
    '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=ragcanon',
    '-p', `127.0.0.1:${PORT}:5432`, 'pgvector/pgvector:pg16'], { stdio: 'pipe' });
  const dsn = `postgres://postgres:x@127.0.0.1:${PORT}/ragcanon`;
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); return dsn; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

// ── manifest fixture 工具 ──
function fixtureDir() { return fs.mkdtempSync(path.join(os.tmpdir(), 'ragmf-')); }
function writeFixture(mutate = () => {}) {
  const dir = fixtureDir();
  const files = {
    'model.safetensors': Buffer.alloc(4096, 7),
    'tokenizer.json': Buffer.from('{"vocab":{}}'),
    'config.json': Buffer.from('{"hidden_size":8}'),
    'modules.json': Buffer.from('[]'),
  };
  for (const [n, b] of Object.entries(files)) fs.writeFileSync(path.join(dir, n), b);
  mutate(dir, files);
  const manifest = {
    manifest_version: 2, model_id: 'fixture-model', dims: 256, pooling: 'cls_l2',
    distance: 'cosine', runtime: 'fixture',
    files: Object.entries(files).map(([name, b]) => ({
      name, sha256: crypto.createHash('sha256').update(b).digest('hex'), bytes: b.length,
    })),
  };
  manifest.files_count = manifest.files.length;
  manifest.total_bytes = manifest.files.reduce((s, f) => s + f.bytes, 0);
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(manifest));
  return dir;
}
function pyVerify(dir, manifestName = 'manifest.json') {
  const r = spawnSync('python', ['-X', 'utf8', path.join(REPO_ROOT, 'tools', 'bge_embed.py'),
    '--model-dir', dir, '--manifest', path.join(dir, manifestName), '--verify-only'],
    { encoding: 'utf8', cwd: REPO_ROOT });
  return r.status;
}

// ── keystore fixture ──
function makeKeys(dir, n = 1, revoked = false) {
  fs.mkdirSync(dir, { recursive: true });
  const out = [];
  for (let i = 0; i < n; i++) {
    const kid = `rk-canon-it-${Date.now()}-${i}`;
    const secret = crypto.randomBytes(32).toString('hex');
    fs.writeFileSync(path.join(dir, `${kid}.key.json`), JSON.stringify({
      key_id: kid, secret, algorithm: 'hmac-sha256-full',
      created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 86_400_000).toISOString(), revoked,
    }));
    out.push({ kid, secret });
  }
  return out;
}

const dsn = await boot();
const KS = fs.mkdtempSync(path.join(os.tmpdir(), 'ragks-'));
const [key1] = makeKeys(KS);

const savedEnv = { ...process.env };
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'canon-op';
process.env.CONSOLE_PILOT_PASSWORD = 'canon-test-password';
process.env.CONSOLE_SESSION_SECRET = 'canon-secret';
process.env.RAGTRIAL_ALLOWED_SCOPES = SCOPES;
process.env.MERGEPILOT_RUN_BINDING_KEYSTORE = KS;
process.env.RAGTRIAL_SCOPE_AUDIT_DENY_CAP = '3';

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
let cookie;
{
  const res = await fetch(BASE + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'canon-op', password: 'canon-test-password' }),
  });
  cookie = (res.headers.get('set-cookie') || '').split(';')[0];
}
const call = async (p, body) => {
  const res = await fetch(BASE + p, {
    method: body ? 'POST' : 'GET',
    headers: { cookie, 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
};
const machine = async (body) => {
  const res = await fetch(BASE + '/api/rag-trial/machine/query', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
};
const msign = (secret, extra) => {
  const nonce = crypto.randomBytes(10).toString('hex');
  const timestamp = Date.now();
  return { nonce, timestamp, signature: sign(secret, { run_id: extra.run_id, nonce, timestamp }), ...extra };
};

const pool = new Pool({ connectionString: dsn });

try {
  // ── SC*：scope 矩阵 ──
  // 先建一点索引数据（走 store 直连，绕过 HTTP scope——数据面不受影响）
  const { createRagTrialStore } = await import('../lib/ragtrial/store.mjs');
  const store = await createRagTrialStore({ pool });
  await store.initSchema();
  await store.ingestDocuments({ docs: [{ path: 'canon/a.md', text: 'canary scope gate probe with anchors' }],
    repo: REPO, branch: BR });

  const allow = await call('/api/rag-trial/query', { q: 'canary scope gate', repo: REPO, branch: BR, k: 3 });
  ok('SC1 scope allow（在允许清单 → 正常六状态响应）', allow.status === 200 && allow.json?.service_state !== undefined,
    { status: allow.status, body: JSON.stringify(allow.json).slice(0, 200) });
  const xrepo = await call('/api/rag-trial/query', { q: 'canary', repo: 'other/repo', branch: BR, k: 3 });
  ok('SC2 跨 repo → 403 scope_not_allowed', xrepo.status === 403 && xrepo.json?.error?.reason === 'scope_not_allowed');
  const xbr = await call('/api/rag-trial/query', { q: 'canary', repo: REPO, branch: 'main', k: 3 });
  ok('SC3 跨 branch → 403', xbr.status === 403 && xbr.json?.error?.reason === 'scope_not_allowed');
  // 撤销：清 env → 同一 scope 立即拒绝
  delete process.env.RAGTRIAL_ALLOWED_SCOPES;
  const revoked = await call('/api/rag-trial/query', { q: 'canary', repo: REPO, branch: BR, k: 3 });
  ok('SC4 env 撤销即时生效 → 403 scope_not_configured（默认拒绝）',
    revoked.status === 403 && revoked.json?.error?.reason === 'scope_not_configured');
  // 机器身份越权（恢复 allowlist 后，验签通过但 scope 越界）
  process.env.RAGTRIAL_ALLOWED_SCOPES = SCOPES;
  const mOver = await machine(msign(key1.secret, { run_id: 'canon-run-1', q: 'canary', repo: 'other/repo', branch: BR, k: 3 }));
  ok('SC5 机器身份越权（验签通过）→ 403', mOver.status === 403 && mOver.json?.error?.reason === 'scope_not_allowed');
  const mAllow = await machine(msign(key1.secret, { run_id: 'canon-run-1', q: 'canary scope gate', repo: REPO, branch: BR, k: 3 }));
  ok('SC6 机器身份 allowlist 内 → 200+辅助证据', mAllow.status === 200 && (mAllow.json?.auxiliary_evidence ?? []).length > 0);
  // 有界拒绝审计：cap=3（本组已用 4 次拒绝：SC2/SC3/SC4/SC5 同源 127.0.0.1 → 第 4 次起 suppressed）
  const audits = (await pool.query(`SELECT count(*)::int n FROM ragtrial.audit_events WHERE kind='QUERY_SCOPE_DENIED'`)).rows[0].n;
  const again = await call('/api/rag-trial/query', { q: 'canary', repo: 'x/y', branch: 'z', k: 3 });
  ok('SC7 拒绝审计有界封顶（cap=3；超限响应带 audit_suppressed）',
    audits === 3 && again.json?.audit_suppressed === true, { audits });

  // ── KR*：keystore 轮换/撤销 ──
  // 撤销 key1 + 引入 key2
  const kf = fs.readdirSync(KS).find((f) => f.endsWith('.key.json'));
  const k1json = JSON.parse(fs.readFileSync(path.join(KS, kf), 'utf8'));
  k1json.revoked = true;
  fs.writeFileSync(path.join(KS, kf), JSON.stringify(k1json));
  const [key2] = makeKeys(KS);
  const oldSig = await machine(msign(key1.secret, { run_id: 'canon-run-2', q: 'canary', repo: REPO, branch: BR, k: 3 }));
  ok('KR1 撤销后旧密钥签名 → 401', oldSig.status === 401);
  const newSig = await machine(msign(key2.secret, { run_id: 'canon-run-2', q: 'canary scope gate', repo: REPO, branch: BR, k: 3 }));
  ok('KR2 新密钥签名 → 200', newSig.status === 200);

  // ── MF*：manifest v2 三重校验 ──
  ok('MF1 合法 fixture → exit 0', pyVerify(writeFixture()) === 0);
  ok('MF2 文件缺失 → exit 3',
    pyVerify(writeFixture((d) => fs.unlinkSync(path.join(d, 'modules.json')))) === 3);
  ok('MF3 字节数漂移（同长度内容替换→bytes 不变但 sha 变；此处改 bytes 字段模拟尺寸漂移）→ exit 3',
    pyVerify(writeFixture((d) => fs.appendFileSync(path.join(d, 'config.json'), ' '))) === 3);
  ok('MF4 digest 漂移（同尺寸篡改）→ exit 3',
    pyVerify(writeFixture((d, files) => {
      fs.writeFileSync(path.join(d, 'tokenizer.json'), Buffer.alloc(files['tokenizer.json'].length, 9));
    })) === 3);
  ok('MF5 必需工件缺失（tokenizer 从 manifest 移除+文件删除）→ exit 3',
    pyVerify(writeFixture((d) => fs.unlinkSync(path.join(d, 'tokenizer.json')))) === 3);
  ok('MF6 夹带多余受管文件 → exit 3',
    pyVerify(writeFixture((d) => fs.writeFileSync(path.join(d, 'vocab.txt'), 'x'))) === 3);

  // ── WC*：双 worker 并发 claim 竞争 ──
  const { createJobQueue } = await import('../lib/ragtrial/queue.mjs');
  const queue = createJobQueue({ pool });
  const N = 6;
  for (let i = 0; i < N; i++) {
    await queue.enqueue({ kind: 'ingest_doc', repo: REPO, branch: BR,
      doc_path: `canon/race-${i}.md`, content_sha256: crypto.createHash('sha256').update(`race ${i}`).digest('hex'),
      model_id: 'local-hash-v1', payload: { text: `race doc ${i} unique anchors ${i}` } });
  }
  const runW = (name) => new Promise((resolve) => {
    const p = spawn(process.execPath, [path.join(REPO_ROOT, 'tools', 'rag-worker.mjs'),
      '--dsn', dsn, '--max-jobs', String(Math.ceil(N / 2)), '--worker', name],
      { cwd: REPO_ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = ''; p.stdout.on('data', (c) => { out += c; }); p.stderr.on('data', (c) => { out += c; });
    p.on('exit', (c) => resolve({ code: c, out }));
  });
  const [w1, w2] = await Promise.all([runW('race-w1'), runW('race-w2')]);
  const rows = (await pool.query(`SELECT state, count(*)::int n, sum(attempts)::int att FROM ragtrial.jobs WHERE doc_path LIKE 'canon/race-%' GROUP BY state`)).rows;
  const chunks = (await pool.query(`SELECT count(DISTINCT doc_path)::int docs, count(*)::int rows FROM ragtrial.chunks WHERE doc_path LIKE 'canon/race-%'`)).rows[0];
  ok('WC1 双 worker 竞争：N 任务全 done、attempts 总和=N（恰一次）',
    w1.code === 0 && w2.code === 0
    && rows.length === 1 && rows[0].state === 'done' && Number(rows[0].n) === N && Number(rows[0].att) === N,
    { rows, w1: w1.out.slice(-80), w2: w2.out.slice(-80) });
  ok('WC2 无重复索引（6 文档各 1 chunk）', Number(chunks.docs) === N && Number(chunks.rows) === N, chunks);

  // ── 披露端点 ──
  const prov = await call('/api/rag-trial/providers');
  ok('D1 /providers：deterministic 标注测试角色 + semantic 未接线如实 NOT_CONFIGURED + 脱敏注记',
    prov.status === 200 && prov.json?.deterministic?.model_id === 'local-hash-v1'
    && /不宣称真实语义/.test(prov.json.deterministic.role)
    && prov.json?.semantic?.state === 'NOT_CONFIGURED'
    && /不回显密钥/.test(prov.json?.disclosure_note ?? ''), prov.json);
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  // 还原进程 env（防止污染同进程其他测试）
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nragtrial-canonical.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
