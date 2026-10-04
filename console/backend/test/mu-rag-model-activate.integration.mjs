#!/usr/bin/env node
// console/backend/test/mu-rag-model-activate.integration.mjs — RAG-model-install PR3/5：
// 激活/回退全矩阵（伪 sidecar 三重门：health/manifest 字节 pin/embed 维度冒烟）。
// 覆盖任务书五.10-12/15/17-18/21：sidecar 失败/READY 才能激活/双向切换/并发激活单赢家/
// RBAC/CSRF/错误码/local-hash 检索零回归。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import http from 'node:http';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');
const { createConsole } = await import('../server.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 220) : ''}`); }
};
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

// ── 测试工件：小模型两文件 ──
const F1 = Buffer.from('model-config-bytes\n');
const F2 = crypto.randomBytes(2048);
const tmpRoot = await fsp.mkdtemp(path.join(os.tmpdir(), 'rmi3-'));
const maniDir = path.join(tmpRoot, 'manifests');
await fsp.mkdir(maniDir, { recursive: true });

// ── 伪 sidecar（三重门端点；行为可切换以测失败分支）──
let sidecarMode = 'ok'; // ok | health_down | manifest_mismatch | dims_bad | embed_500
let sidecarManifestBytes = null;
const sidecar = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  if (u.pathname.endsWith('/health')) {
    if (sidecarMode === 'health_down') { res.writeHead(503).end(); return; }
    res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"ok":true}'); return;
  }
  if (u.pathname.endsWith('/manifest')) {
    if (!sidecarManifestBytes) { res.writeHead(500).end(); return; }
    const body = sidecarMode === 'manifest_mismatch'
      ? Buffer.from(sidecarManifestBytes.toString('utf8').replace('"dims": 8', '"dims": 999'))
      : sidecarManifestBytes;
    res.writeHead(200, { 'content-type': 'application/json' }); res.end(body); return;
  }
  if (u.pathname.endsWith('/embed') && req.method === 'POST') {
    if (sidecarMode === 'embed_500') { res.writeHead(500).end(); return; }
    const dims = sidecarMode === 'dims_bad' ? 7 : 8;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ data: [{ embedding: Array(dims).fill(0.1) }] })); return;
  }
  res.writeHead(404).end();
});
await new Promise((r) => sidecar.listen(0, '127.0.0.1', r));
const SC_PORT = sidecar.address().port;

// ── 伪官方源（下载）──
const dlFiles = { 'cfg.json': F1, 'w.bin': F2 };
const dlSrv = http.createServer((req, res) => {
  const fp = new URL(req.url, 'http://x').searchParams.get('FilePath');
  const buf = dlFiles[fp];
  if (!buf) { res.writeHead(404).end(); return; }
  res.writeHead(200, { 'content-length': buf.length }); res.end(buf);
});
await new Promise((r) => dlSrv.listen(0, '127.0.0.1', r));
const DL_PORT = dlSrv.address().port;

const MANIFEST = {
  manifest_version: 'testm-v1', model_key: 'testm', model_id: 'T/testm', dims: 8,
  pooling: 'cls_l2', distance: 'cosine', license: 'MIT',
  source: { official_channel: 'test', allowed_download_hosts: ['127.0.0.1'],
    download_url_template: `http://127.0.0.1:${DL_PORT}/repo?FilePath={path}&Revision=deadbeef`,
    files_revision: 'deadbeef', repo_revision_short: 'deadbee' },
  files: [
    { path: 'cfg.json', sha256: sha(F1), bytes: F1.length },
    { path: 'w.bin', sha256: sha(F2), bytes: F2.length },
  ],
  total_bytes: F1.length + F2.length,
};
await fsp.writeFile(path.join(maniDir, 'testm.modelscope.manifest.json'), JSON.stringify(MANIFEST, null, 1));

const CTR = `mu-rmi3-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17900 + Math.floor(Math.random() * 40);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'pgvector/pgvector:pg16'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'rmi3-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.RMI_MANIFEST_DIR = maniDir;
process.env.RAG_MODEL_ROOT = path.join(tmpRoot, 'models');
process.env.RAGTRIAL_EMBED_ENDPOINT = `http://127.0.0.1:${SC_PORT}/embed`;
const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;

const login = async (subject) => {
  const r = await fetch(`${BASE}/api/mu/auth/login`, { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }) });
  const cookie = (r.headers.get('set-cookie') || '').split(';')[0];
  return { cookie, csrf: (await r.json().catch(() => null))?.csrf ?? '' };
};
const call = async (sess, method, p, opts = {}) => {
  const r = await fetch(`${BASE}${p}`, { method,
    headers: { ...(opts.body ? { 'content-type': 'application/json' } : {}),
      ...(sess?.cookie ? { cookie: sess.cookie } : {}),
      ...(opts.csrf && sess?.csrf ? { 'x-csrf-token': sess.csrf } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  return { status: r.status, body: await r.json().catch(() => null) };
};

const storeMod = await import('../lib/multiuser/rag-model-install.mjs');
const dlMod = await import('../lib/multiuser/rag-model-download.mjs');
const actMod = await import('../lib/multiuser/rag-model-activate.mjs');
const registryMod = await import('../lib/ragtrial/store.mjs');
const rmiStore = {
  ...storeMod,
  loadModelManifest: (k) => JSON.parse(fs.readFileSync(path.join(maniDir, `${k}.modelscope.manifest.json`), 'utf8')),
  listInstallableModels: () => ['testm'],
};
const events = [];
const onEvent = (kind, detail) => events.push({ kind, detail });
const ROOT = process.env.RAG_MODEL_ROOT;

try {
  for (let i = 0; i < 60; i++) {
    await fetch(`${BASE}/api/mu/session`).catch(() => {});
    try { if (Number((await pool.query(`SELECT max(version) v FROM mu.schema_migrations`)).rows[0].v) >= 19) break; } catch { /* */ }
    await new Promise((r) => setTimeout(r, 700));
  }
  const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  const mkUser = async (tenantId, loginName, role) => {
    const u = (await pool.query(`INSERT INTO mu.app_user (login, display_name) VALUES ($1,$1) RETURNING user_id`, [loginName])).rows[0];
    await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,$3)`, [tenantId, u.user_id, role]);
    await pool.query(`INSERT INTO mu.external_identity (provider, subject, user_id) VALUES ('fixture',$1,$2)`, [`fixture:${loginName}`, u.user_id]);
  };
  await mkUser(T1, 'a3-admin', 'platform_admin');
  await mkUser(T1, 'a3-contrib', 'contributor');
  const admin = await login('fixture:a3-admin');
  const contrib = await login('fixture:a3-contrib');

  // 安装到 READY（复用 PR2 引擎直驱）
  await rmiStore.ensureInstallRow(pool, { tenantId: T1, modelKey: 'testm' });
  const inst = await dlMod.runInstall({ pool, tenantId: T1, modelKey: 'testm',
    manifest: rmiStore.loadModelManifest('testm'), modelRoot: ROOT, storeMod: rmiStore, onEvent });
  ok('A0 安装到 READY（前置）', inst.ok && inst.finalState === 'READY', inst);

  // A1：READY 门槛（未安装好时激活 409——先造 UNINSTALLED 第二租户）
  const T2 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('a3-t2','A3 T2') RETURNING tenant_id`)).rows[0].tenant_id;
  const u2 = (await pool.query(`INSERT INTO mu.app_user (login, display_name) VALUES ('a3-t2admin','T2') RETURNING user_id`)).rows[0].user_id;
  await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,'platform_admin')`, [T2, u2]);
  await pool.query(`INSERT INTO mu.external_identity (provider, subject, user_id) VALUES ('fixture','fixture:a3-t2admin',$1)`, [u2]);
  const admin2 = await login('fixture:a3-t2admin');
  const notReady = await call(admin2, 'POST', '/api/mu/rag-model/activate', { csrf: true, body: { model_key: 'testm' } });
  ok('A1 非 READY 激活 → 409 illegal_state', notReady.status === 409
    && String(notReady.body?.error?.reason).startsWith('illegal_state'), notReady.body);

  // A2：sidecar health 挂 → SIDECAR_START_FAILED（422）
  sidecarMode = 'health_down';
  const rHealth = await actMod.activateModel({ pool, tenantId: T1, modelKey: 'testm',
    manifest: rmiStore.loadModelManifest('testm'), modelRoot: ROOT, storeMod: rmiStore,
    registryMod, endpoint: process.env.RAGTRIAL_EMBED_ENDPOINT, onEvent });
  const rowH = await rmiStore.getInstall(pool, { tenantId: T1, modelKey: 'testm' });
  ok('A2a health 挂 → SIDECAR_START_FAILED+错误码', rHealth.ok === false && rHealth.reason === 'sidecar_probe_failed'
    && rowH.state === 'SIDECAR_START_FAILED' && rowH.last_error_code.includes('health'), { r: rHealth.reason, s: rowH.state, e: rowH.last_error_code });
  await rmiStore.transitionInstall(pool, { tenantId: T1, modelKey: 'testm', from: 'SIDECAR_START_FAILED', to: 'READY' });

  // A3：manifest 字节不匹配 → 拒激活
  sidecarManifestBytes = actMod.buildSidecarManifest(rmiStore.loadModelManifest('testm')).bytes;
  sidecarMode = 'manifest_mismatch';
  const rMM = await actMod.activateModel({ pool, tenantId: T1, modelKey: 'testm',
    manifest: rmiStore.loadModelManifest('testm'), modelRoot: ROOT, storeMod: rmiStore,
    registryMod, endpoint: process.env.RAGTRIAL_EMBED_ENDPOINT, onEvent });
  const rowMM = await rmiStore.getInstall(pool, { tenantId: T1, modelKey: 'testm' });
  ok('A3 manifest 篡改 → manifest_sha_mismatch 拒激活', rMM.ok === false
    && rowMM.state === 'SIDECAR_START_FAILED' && rowMM.last_error_code.includes('manifest_sha_mismatch'), rowMM.last_error_code);
  await rmiStore.transitionInstall(pool, { tenantId: T1, modelKey: 'testm', from: 'SIDECAR_START_FAILED', to: 'READY' });

  // A4：维度冒烟失败 → 拒激活
  sidecarMode = 'dims_bad';
  const rDims = await actMod.activateModel({ pool, tenantId: T1, modelKey: 'testm',
    manifest: rmiStore.loadModelManifest('testm'), modelRoot: ROOT, storeMod: rmiStore,
    registryMod, endpoint: process.env.RAGTRIAL_EMBED_ENDPOINT, onEvent });
  ok('A4 维度不符 → dims_mismatch 拒激活', rDims.ok === false
    && rDims.detail?.error_code === 'dims_mismatch', rDims.detail);
  await rmiStore.transitionInstall(pool, { tenantId: T1, modelKey: 'testm', from: 'SIDECAR_START_FAILED', to: 'READY' });

  // A4b（rc.10 修复 a）：SIDECAR_START_FAILED→verify-only→READY 全链——
  // sidecar 修复后"重新校验"直达 READY，不再只能 cancel→全量重下
  {
    sidecarMode = 'health_down';
    await actMod.activateModel({ pool, tenantId: T1, modelKey: 'testm',
      manifest: rmiStore.loadModelManifest('testm'), modelRoot: ROOT, storeMod: rmiStore,
      registryMod, endpoint: process.env.RAGTRIAL_EMBED_ENDPOINT, onEvent });
    const rowSf = await rmiStore.getInstall(pool, { tenantId: T1, modelKey: 'testm' });
    ok('A4b-0 前置：激活失败落 SIDECAR_START_FAILED+错误码', rowSf.state === 'SIDECAR_START_FAILED'
      && !!rowSf.last_error_code, { s: rowSf.state });
    const v = await call(admin, 'POST', '/api/mu/rag-model/install/verify',
      { csrf: true, body: { model_key: 'testm' } });
    const rowV = await rmiStore.getInstall(pool, { tenantId: T1, modelKey: 'testm' });
    ok('A4b-1 verify-only 全链 → 200+READY（免 cancel→重下 2.27GB）', v.status === 200
      && v.body?.finalState === 'READY' && rowV.state === 'READY', { st: v.status, b: v.body, s: rowV.state });
    ok('A4b-2 READY 回写进度=manifest 总量+错误码清 NULL', Number(rowV.downloaded_bytes) === MANIFEST.total_bytes
      && Number(rowV.total_bytes) === MANIFEST.total_bytes && rowV.last_error_code === null,
      { d: rowV.downloaded_bytes, e: rowV.last_error_code });
  }

  // A5：三重门全过 → ACTIVE（v19+注册表双切）
  sidecarMode = 'ok';
  const rAct = await actMod.activateModel({ pool, tenantId: T1, modelKey: 'testm',
    manifest: rmiStore.loadModelManifest('testm'), modelRoot: ROOT, storeMod: rmiStore,
    registryMod, endpoint: process.env.RAGTRIAL_EMBED_ENDPOINT, onEvent });
  const rowAct = await rmiStore.getInstall(pool, { tenantId: T1, modelKey: 'testm' });
  const activeModel = (await pool.query(`SELECT model_id, provider_kind, dims FROM ragtrial.models WHERE active`)).rows[0];
  ok('A5a 激活成功 → ACTIVE+provider testm+激活时间', rAct.ok && rowAct.state === 'ACTIVE'
    && rowAct.active_provider === 'testm' && !!rowAct.activated_at, { s: rowAct.state });
  ok('A5b ragtrial 注册表双切（active=testm/remote/dims=8）', activeModel?.model_id === 'testm'
    && activeModel?.provider_kind === 'remote' && Number(activeModel?.dims) === 8, activeModel);
  const written = await fsp.readFile(path.join(ROOT, 'testm', 'manifest.json'));
  ok('A5c sidecar manifest 落模型目录（字节=pin 源）', sha(written) === sha(sidecarManifestBytes));

  // A6：并发首激活单赢家（先回退制造 READY 起点再并发两路）
  await actMod.rollbackToLocal({ pool, tenantId: T1, modelKey: 'testm', storeMod: rmiStore, registryMod, onEvent });
  const [p1, p2] = await Promise.all([
    actMod.activateModel({ pool, tenantId: T1, modelKey: 'testm', manifest: rmiStore.loadModelManifest('testm'),
      modelRoot: ROOT, storeMod: rmiStore, registryMod, endpoint: process.env.RAGTRIAL_EMBED_ENDPOINT, onEvent }),
    actMod.activateModel({ pool, tenantId: T1, modelKey: 'testm', manifest: rmiStore.loadModelManifest('testm'),
      modelRoot: ROOT, storeMod: rmiStore, registryMod, endpoint: process.env.RAGTRIAL_EMBED_ENDPOINT, onEvent }),
  ]);
  const rowA6 = await rmiStore.getInstall(pool, { tenantId: T1, modelKey: 'testm' });
  ok('A6 并发首激活恰一非幂等且终态 ACTIVE', p1.ok && p2.ok && (p1.idempotent !== p2.idempotent)
    && rowA6.state === 'ACTIVE' && rowA6.active_provider === 'testm', { a: p1.idempotent, b: p2.idempotent, s: rowA6.state });

  // A7：回退 → READY+local-hash+注册表回 local
  const rBack = await actMod.rollbackToLocal({ pool, tenantId: T1, modelKey: 'testm',
    storeMod: rmiStore, registryMod, onEvent });
  const rowBack = await rmiStore.getInstall(pool, { tenantId: T1, modelKey: 'testm' });
  const localModel = (await pool.query(`SELECT model_id, provider_kind FROM ragtrial.models WHERE active`)).rows[0];
  ok('A7a 回退 → READY+local-hash（安装保留）', rBack.ok && rowBack.state === 'READY'
    && rowBack.active_provider === 'local-hash-v1' && rowBack.activated_at !== null);
  ok('A7b 注册表回 local-hash-v1', localModel?.model_id === 'local-hash-v1' && localModel?.provider_kind === 'local');
  const rBack2 = await actMod.rollbackToLocal({ pool, tenantId: T1, modelKey: 'testm',
    storeMod: rmiStore, registryMod, onEvent });
  ok('A7c 重复回退幂等', rBack2.ok && rBack2.idempotent === true);

  // A8：HTTP 面 RBAC/CSRF/404
  ok('A8a 未认证激活 → 401', (await call(null, 'POST', '/api/mu/rag-model/activate',
    { csrf: true, body: {} })).status === 401);
  ok('A8b contributor 激活 → 403', (await call(contrib, 'POST', '/api/mu/rag-model/activate',
    { csrf: true, body: { model_key: 'testm' } })).status === 403);
  ok('A8c 缺 CSRF 回退 → 403', (await call(admin, 'POST', '/api/mu/rag-model/rollback',
    { body: {} })).body?.error?.reason === 'csrf_required');
  ok('A8d 未知模型激活 → 404', (await call(admin, 'POST', '/api/mu/rag-model/activate',
    { csrf: true, body: { model_key: 'nope' } })).status === 404);

  // A9：local-hash 检索零回归（回退态下 ragtrial 默认模型=local）
  const reg = await pool.query(`SELECT model_id FROM ragtrial.models WHERE active`);
  ok('A9 回退态默认模型=local-hash-v1（检索路径零回归）', reg.rows[0]?.model_id === 'local-hash-v1');

  // A10：审计事件齐+脱敏
  const kinds = new Set(events.map((e) => e.kind));
  for (const k of ['RAG_MODEL_ACTIVATED', 'RAG_MODEL_ROLLED_BACK', 'RAG_MODEL_SIDECAR_FAILED']) {
    ok(`A10 审计含 ${k}`, kinds.has(k));
  }
  const raw = JSON.stringify(events);
  ok('A10b 审计零敏感（无 embedding 数值/正文/secret）', !raw.includes('activation-probe')
    && !raw.includes('"0.1"') && !/(sk|ghp)_[A-Za-z0-9]{10,}/.test(raw));
} finally {
  try { server.close(); } catch { /* */ }
  sidecar.close(); dlSrv.close();
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
  try { fs.rmSync(tmpRoot, { recursive: true, force: true }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
