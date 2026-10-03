#!/usr/bin/env node
// console/backend/test/mu-rag-model-wave.integration.mjs — RAG-model-install PR5/5：
// 波级收尾矩阵：磁盘不足 fail-closed + manifest 真源容器锚点 + 状态机全局不变量 +
// C 波兼容声明（零调用留痕伪造）。运行：node console/backend/test/本文件（自起一次性 PG）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import os from 'node:os';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

const tmpRoot = await fsp.mkdtemp(path.join(os.tmpdir(), 'rmi5-'));
const maniDir = path.join(tmpRoot, 'manifests');
await fsp.mkdir(maniDir, { recursive: true });

// 伪官方源 + 超大 manifest（磁盘预检必拒）
const F = { 'c.bin': crypto.randomBytes(1024) };
const srv = http.createServer((req, res) => {
  const fp = new URL(req.url, 'http://x').searchParams.get('FilePath');
  const buf = F[fp];
  if (!buf) { res.writeHead(404).end(); return; }
  res.writeHead(200, { 'content-length': buf.length }); res.end(buf);
});
await new Promise((r) => srv.listen(0, '127.0.0.1', r));
const PORT = srv.address().port;
const HUGE = 8 * 1024 ** 4; // 8 TiB——任何磁盘必不足
const writeMani = (total) => fsp.writeFile(path.join(maniDir, 'huge.modelscope.manifest.json'), JSON.stringify({
  manifest_version: 'huge-v1', model_key: 'huge', dims: 8, license: 'MIT',
  source: { official_channel: 'test', allowed_download_hosts: ['127.0.0.1'],
    download_url_template: `http://127.0.0.1:${PORT}/repo?FilePath={path}&Revision=dd`,
    files_revision: 'dd', repo_revision_short: 'd' },
  files: [{ path: 'c.bin', sha256: sha(F['c.bin']), bytes: F['c.bin'].length }],
  total_bytes: total,
}, null, 1));

const CTR = `mu-rmi5-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 19600 + Math.floor(Math.random() * 40);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.RMI_MANIFEST_DIR = maniDir; // 伪官方源注入（W3 以子进程独立验证仓库锚点）
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const store = await createMuStore({ pool });
await store.initSchema();
await store.bootstrap();

const rmiStore = await import('../lib/multiuser/rag-model-install.mjs');
const dl = await import('../lib/multiuser/rag-model-download.mjs');
const st = {
  ...rmiStore,
  loadModelManifest: (k) => JSON.parse(fs.readFileSync(path.join(maniDir, `${k}.modelscope.manifest.json`), 'utf8')),
  listInstallableModels: () => ['huge'],
};

try {
  const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;

  // W1：磁盘不足提前拒绝（开始前，零字节落盘）
  await writeMani(HUGE);
  const manifest = st.loadModelManifest('huge');
  const disk = await dl.checkDiskSpace(path.join(tmpRoot, 'models'), manifest.total_bytes);
  ok('W1a checkDiskSpace 判不足', disk.ok === false, disk);
  await st.ensureInstallRow(pool, { tenantId: T1, modelKey: 'huge' });
  const r1 = await dl.runInstall({ pool, tenantId: T1, modelKey: 'huge', manifest,
    modelRoot: path.join(tmpRoot, 'models'), storeMod: st, onEvent: () => {} });
  const row1 = await st.getInstall(pool, { tenantId: T1, modelKey: 'huge' });
  ok('W1b 引擎开始前拒（INSUFFICIENT_DISK+错误码+零下载字节）', r1.ok === false
    && r1.reason === 'insufficient_disk' && row1.state === 'INSUFFICIENT_DISK'
    && Number(row1.downloaded_bytes) === 0 && !!row1.last_error_code, { s: row1.state });

  // W2：磁盘恢复后同一路径续装成功（INSUFFICIENT_DISK→DOWNLOADING 边）
  await writeMani(F['c.bin'].length);
  const m2 = st.loadModelManifest('huge');
  const r2 = await dl.runInstall({ pool, tenantId: T1, modelKey: 'huge', manifest: m2,
    modelRoot: path.join(tmpRoot, 'models'), storeMod: st, onEvent: () => {} });
  ok('W2 磁盘恢复后同键续装 → READY', r2.ok && r2.finalState === 'READY', r2);

  // W3：manifest 真源仓库锚点（子进程不设 env——验证候选锚点落仓库 deploy/rag-model-install）
  {
    const modPath = path.resolve(HERE, '../lib/multiuser/rag-model-install.mjs').split(path.sep).join('/');
    const out = execFileSync('node', ['--input-type=module', '-e',
      `const m = await import('file:///' + process.env.RMI5_MOD);
       const list = m.listInstallableModels();
       const mm = list.includes('bge-m3') ? m.loadModelManifest('bge-m3') : null;
       console.log(JSON.stringify({ list, rev: mm?.source?.files_revision, files: mm?.files?.length, lic: mm?.license }));`],
      { env: { ...process.env, RMI_MANIFEST_DIR: '', RMI5_MOD: modPath }, encoding: 'utf8' });
    const j = JSON.parse(out);
    ok('W3 仓库真源锚点（bge-m3 清单+官方 revision+4 文件+MIT）', j.rev === 'e44369c5623cc146f016da906583db4ee0e3488d'
      && j.files === 4 && j.lic === 'MIT', j);
  }

  // W4：状态机全局不变量（DB CHECK 兜底）
  const bad = await pool.query(
    `UPDATE mu.rag_model_install SET state='BOGUS' WHERE tenant_id=$1`, [T1])
    .then(() => false, () => true);
  ok('W4 非法状态被 DB CHECK 拒（任何路径不可绕）', bad === true);

  // W5：C 波兼容——零调用留痕伪造（本波不产生任何 ragtrial.query_log 行/调用计数）
  const ql = (await pool.query(`SELECT count(*)::int c FROM ragtrial.query_log`).catch(() => ({ rows: [{ c: -1 }] }))).rows[0].c;
  ok('W5 零查询留痕伪造（query_log 未被安装/激活触碰）', Number(ql) === 0 || ql === -1, ql);

  // W6：零 GitHub 写
  const gh = (await pool.query(`SELECT count(*)::int c FROM mu.audit_event
    WHERE detail::text ILIKE '%api.github.com%' OR detail::text ILIKE '%/merge%'`)).rows[0].c;
  ok('W6 零 GitHub 写', gh === 0);
} finally {
  srv.close();
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
  try { fs.rmSync(tmpRoot, { recursive: true, force: true }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
