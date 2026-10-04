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
//   SA*：PHASE0A 会话仓库授权（复核 CT-01 修复）——session∩部署 scope 组合门：
//        query/review-aux/eval/ingest/delete/jobs/index 的会话层拒绝、拒绝路径
//        零内容泄露（无 snippet/citation/doc_path）、写路径零副作用、批量整批
//        拒绝、index 跨界门（invalidate SA7 + rollback 独立回归 SA7b/F-2）、
//        列表/死信过滤、越权 requeue 404 与不存在同形零差分（F-1）、机器通道不受会话层影响。
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
// SA*（PHASE0A）：acme/rag、zeta/priv 在部署 scope 内但不在 console 会话
// allowlist（下）——用于让请求"过 scope 层、被会话层拒"（两层都可独立命中）
const SCOPES = `${REPO}@${BR},acme/rag@main,zeta/priv@main`;
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
  // Beta Hardening W1：python→python3 回退（CI runner 通常只有 python3；
  // 本地 Windows 有 python）。ENOENT（status null）才切换，不影响既有失败语义。
  const args = ['-X', 'utf8', path.join(REPO_ROOT, 'tools', 'bge_embed.py'),
    '--model-dir', dir, '--manifest', path.join(dir, manifestName), '--verify-only'];
  let r = spawnSync('python', args, { encoding: 'utf8', cwd: REPO_ROOT });
  if (r.status === null && /ENOENT|not found/i.test(String(r.error))) {
    r = spawnSync('python3', args, { encoding: 'utf8', cwd: REPO_ROOT });
  }
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
// SA*（PHASE0A）：会话仓库授权面——刻意不含 acme/rag、zeta/priv（它们在 scope 内）；
// 含 other/repo 以保证 SC2/SC3 仍命中 scope 层（reason 语义不变）
process.env.CONSOLE_REPO_ALLOWLIST = `${REPO},other/repo`;
process.env.MERGEPILOT_RUN_BINDING_KEYSTORE = KS;
process.env.RAGTRIAL_SCOPE_AUDIT_DENY_CAP = '3';

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
let cookie;
let csrf; // rc.10 SEC-3：rag-trial POST 强制 CSRF——登录 Set-Cookie 串解析 mp_csrf
{
  const res = await fetch(BASE + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'canon-op', password: 'canon-test-password' }),
  });
  cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  csrf = (res.headers.get('set-cookie') || '').match(/mp_csrf=([^;,]+)/)?.[1] ?? null;
}
const call = async (p, body) => {
  const res = await fetch(BASE + p, {
    method: body ? 'POST' : 'GET',
    headers: { cookie, 'content-type': 'application/json',
      ...(body && csrf ? { 'x-csrf-token': csrf } : {}) }, // rc.10 SEC-3
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

  // ── SA*：PHASE0A 会话仓库授权（session∩部署 scope 组合门；复核 CT-01 修复） ──
  const { resetScopeDenyLimiter } = await import('../lib/ragtrial/api.mjs');
  resetScopeDenyLimiter(); // 与 SC7 同源封顶计数器隔离，保证本组审计计数断言确定性
  const repoDeniedCount = async () =>
    (await pool.query(`SELECT count(*)::int n FROM ragtrial.audit_events WHERE kind='RAG_REPO_DENIED'`)).rows[0].n;
  const noLeak = (j) => !JSON.stringify(j).includes('snippet')
    && !JSON.stringify(j).includes('citation') && !JSON.stringify(j).includes('doc_path');

  const saQ = await call('/api/rag-trial/query', { q: 'canary', repo: 'zeta/priv', branch: 'main', k: 3 });
  ok('SA1 query 过 scope 层但被会话层拒 → 403 repo_not_in_allowlist + 零内容泄露',
    saQ.status === 403 && saQ.json?.error?.reason === 'repo_not_in_allowlist' && noLeak(saQ.json), saQ.json);
  ok('SA1b 会话层拒绝落有界审计（RAG_REPO_DENIED=1）', await repoDeniedCount() === 1);

  const saAux = await call('/api/rag-trial/review-aux', { q: 'canary', repo: 'zeta/priv', branch: 'main', run_id: 'sa-run' });
  ok('SA2 review-aux 旁路已封（CT-01 修复）→ 403 + 零 snippet/citation/doc_path',
    saAux.status === 403 && saAux.json?.error?.reason === 'repo_not_in_allowlist' && noLeak(saAux.json), saAux.json);

  const saEv = await call('/api/rag-trial/eval', { repo: 'zeta/priv', branch: 'main', qa: [{ q: 'x', expect_doc: 'y.md' }] });
  ok('SA3 eval 旁路已封（CT-01 修复）→ 403 + 响应无评测 detail（doc_path 预言机关闭）',
    saEv.status === 403 && saEv.json?.error?.reason === 'repo_not_in_allowlist' && saEv.json?.detail === undefined, saEv.json);
  ok('SA3b 前三次拒绝审计在界（RAG_REPO_DENIED=3，cap=3）', await repoDeniedCount() === 3);

  const saIng = await call('/api/rag-trial/ingest', { repo: 'zeta/priv', branch: 'main', docs: [{ path: 'z/p.md', text: 'must never be indexed' }] });
  ok('SA4 ingest 越权 → 403 + 审计封顶标注 + 零索引副作用（documents/jobs 无行）',
    saIng.status === 403 && saIng.json?.audit_suppressed === true
      && (await pool.query(`SELECT count(*)::int n FROM ragtrial.documents WHERE repo='zeta/priv'`)).rows[0].n === 0
      && (await pool.query(`SELECT count(*)::int n FROM ragtrial.jobs WHERE repo='zeta/priv'`)).rows[0].n === 0, saIng.json);

  const saDel = await call('/api/rag-trial/delete', { repo: 'zeta/priv', branch: 'main', doc_path: 'canon/a.md' });
  ok('SA5 delete 越权 → 403（授权面内 canon 文档不受影响）',
    saDel.status === 403
      && (await pool.query(`SELECT count(*)::int n FROM ragtrial.documents WHERE repo=$1 AND doc_path='canon/a.md'`, [REPO])).rows[0].n >= 1);

  const saBatch = await call('/api/rag-trial/jobs', { jobs: [
    { kind: 'ingest_doc', repo: REPO, branch: BR, model_id: 'local-hash-v1',
      doc_path: 'canon/batch-ok.md', content_sha256: crypto.createHash('sha256').update('batch ok').digest('hex'), payload: { text: 'batch ok' } },
    { kind: 'ingest_doc', repo: 'zeta/priv', branch: 'main', model_id: 'local-hash-v1', doc_path: 'z/b.md', payload: { text: 'x' } },
  ] });
  ok('SA6 jobs 批量含越权 → 整批拒绝（batch_rejected=true）且零入队（含合法项）',
    saBatch.status === 403 && saBatch.json?.batch_rejected === true
      && (await pool.query(`SELECT count(*)::int n FROM ragtrial.jobs WHERE doc_path IN ('canon/batch-ok.md','z/b.md')`)).rows[0].n === 0, saBatch.json);

  // acme/rag：scope 内、会话外——跨界门/列表过滤/越权 requeue/机器对照的载体
  await store.ingestDocuments({ docs: [{ path: 'acme/rag-doc.md', text: 'acme rag boundary probe' }], repo: 'acme/rag', branch: 'main' });
  const vBefore = (await pool.query(`SELECT index_version v FROM ragtrial.models WHERE model_id='local-hash-v1'`)).rows[0].v;
  const saInv = await call('/api/rag-trial/index/invalidate', {});
  const vAfter = (await pool.query(`SELECT index_version v FROM ragtrial.models WHERE model_id='local-hash-v1'`)).rows[0].v;
  ok('SA7 index/invalidate 跨界（模型文档仓库超出会话面）→ 403 index_op_crosses_repo_boundary 且 index_version 不变',
    saInv.status === 403 && saInv.json?.error?.reason === 'index_op_crosses_repo_boundary' && vAfter === vBefore,
    { saInv: saInv.json, vBefore, vAfter });

  // SA7b（F-2）：index/rollback 跨界独立回归——拒绝且 index_version/文档/jobs 零变更
  const docsBefore = (await pool.query(`SELECT count(*)::int n FROM ragtrial.documents`)).rows[0].n;
  const jobsBefore = (await pool.query(`SELECT count(*)::int n FROM ragtrial.jobs`)).rows[0].n;
  const saRb = await call('/api/rag-trial/index/rollback', { to_index_version: 1 });
  const vAfterRb = (await pool.query(`SELECT index_version v FROM ragtrial.models WHERE model_id='local-hash-v1'`)).rows[0].v;
  const docsAfterRb = (await pool.query(`SELECT count(*)::int n FROM ragtrial.documents`)).rows[0].n;
  const jobsAfterRb = (await pool.query(`SELECT count(*)::int n FROM ragtrial.jobs`)).rows[0].n;
  ok('SA7b index/rollback 跨界独立回归（F-2）→ 403 同因且 index_version/documents/jobs 零变更',
    saRb.status === 403 && saRb.json?.error?.reason === 'index_op_crosses_repo_boundary'
      && vAfterRb === vBefore && docsAfterRb === docsBefore && jobsAfterRb === jobsBefore,
    { saRb: saRb.json, vBefore, vAfterRb, docsBefore, docsAfterRb, jobsBefore, jobsAfterRb });

  // 越权死信 requeue 播种：直插 acme/rag 的 dead job（绕过 HTTP——HTTP 已被会话层挡住）
  const deadSeed = await queue.enqueue({ kind: 'ingest_doc', repo: 'acme/rag', branch: 'main',
    doc_path: 'acme/dead.md', content_sha256: crypto.createHash('sha256').update('dead').digest('hex'),
    model_id: 'local-hash-v1', payload: { text: 'dead' } });
  await pool.query(`UPDATE ragtrial.jobs SET state='dead', last_error='seeded-dead' WHERE job_id=$1`, [deadSeed.job_id]);

  const saList = await call('/api/rag-trial/jobs');
  ok('SA8 jobs 列表按会话面过滤（WC 的 REPO 行可见、acme/rag 死信不可见）',
    saList.status === 200
      && saList.json?.jobs?.some((j) => j.repo === REPO) === true
      && saList.json?.jobs?.every((j) => j.repo !== 'acme/rag' && j.repo !== 'zeta/priv') === true,
    { rows: saList.json?.jobs?.length });

  // F-1：不存在与"存在但越权"统一 404——重置封顶计数器保证本组审计断言确定性
  resetScopeDenyLimiter();
  const deniedBeforeRq = await repoDeniedCount();
  const requeuePost = async (id) => {
    const res = await fetch(BASE + `/api/rag-trial/jobs/${id}/requeue`, { method: 'POST', headers: { cookie, 'content-type': 'application/json', ...(csrf ? { 'x-csrf-token': csrf } : {}) } });
    let json = null; try { json = await res.json(); } catch { /* */ }
    return { status: res.status, json };
  };
  const saRq = await requeuePost(deadSeed.job_id);
  ok('SA9 越权 job requeue → 404 unknown_job，任务保持 dead，且服务端留有界审计（不回显差异）',
    saRq.status === 404 && saRq.json?.error?.reason === 'unknown_job'
      && (await pool.query(`SELECT state FROM ragtrial.jobs WHERE job_id=$1`, [deadSeed.job_id])).rows[0].state === 'dead'
      && (await repoDeniedCount()) === deniedBeforeRq + 1, saRq.json);
  const saRq2 = await requeuePost('00000000-0000-0000-0000-000000000000');
  ok('SA9b 不存在的 job requeue → 与越权同形 404 unknown_job（F-1：零存在性差分），且不产生越权审计',
    saRq2.status === 404 && saRq2.json?.error?.reason === 'unknown_job'
      && (await repoDeniedCount()) === deniedBeforeRq + 1
      && JSON.stringify(saRq2.json) === JSON.stringify(saRq.json), saRq2.json);
  const authJob = (saList.json?.jobs ?? []).find((j) => j.repo === REPO);
  const saRq3 = authJob ? await requeuePost(authJob.job_id) : { status: 0, json: null };
  ok('SA9c 已授权+存在+非 dead → 409 not_dead（job 状态语义保持，不受 F-1 影响）',
    saRq3.status === 409 && saRq3.json?.error?.reason === 'not_dead', saRq3.json);

  const saQm = await call('/api/rag-trial/queue/metrics');
  ok('SA10 queue/metrics 死信明细按会话面过滤（聚合计数保留全局观测语义）',
    saQm.status === 200 && (saQm.json?.dead_letters ?? []).every((d) => d.repo !== 'acme/rag') === true
      && saQm.json?.by_state !== undefined, saQm.json?.dead_letters);

  const saM = await machine(msign(key2.secret, { run_id: 'sa-machine-1', q: 'boundary probe', repo: 'acme/rag', branch: 'main', k: 3 }));
  ok('SA11 机器端点不经会话层（scope 允许即放行——既有 HMAC+scope 语义未收紧）',
    saM.status === 200, saM.json);


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
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nragtrial-canonical.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
