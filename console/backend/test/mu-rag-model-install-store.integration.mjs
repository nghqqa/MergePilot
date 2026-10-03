#!/usr/bin/env node
// console/backend/test/mu-rag-model-install-store.integration.mjs — v19 store+状态机集成测试
// （RAG-model-install 波 PR1）：manifest 真源/落库幂等/状态机合法迁移/CAS 并发单赢家/
// 激活回退 provider 切换/进度心跳门。运行：node console/backend/test/本文件（自起一次性 PG）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-rmi-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17700 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const store = await createMuStore({ pool });
await store.initSchema();
await store.bootstrap();

const rmi = await import('../lib/multiuser/rag-model-install.mjs');
const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;

try {
  // M1：manifest 真源（仓库版本化文件）
  ok('M1a 可安装模型清单含 bge-m3', rmi.listInstallableModels().includes('bge-m3'));
  const m = rmi.loadModelManifest('bge-m3');
  ok('M1b manifest 四文件+全 sha256(64hex)+官方 revision', m.files.length === 4
    && m.files.every((f) => /^[0-9a-f]{64}$/.test(f.sha256))
    && m.source.files_revision === 'e44369c5623cc146f016da906583db4ee0e3488d'
    && m.license === 'MIT', { rev: m.source.files_revision, lic: m.license });
  ok('M1c 下载模板域名在允许清单（严格源校验）',
    new URL(m.source.download_url_template.replace('{path}', 'config.json')).hostname === 'modelscope.cn'
      && m.source.allowed_download_hosts.includes('modelscope.cn'));
  ok('M1d total_bytes=四文件字节和', m.total_bytes === m.files.reduce((a, f) => a + f.bytes, 0));
  ok('M1e 未知模型 manifest → 抛错（不猜测）', (() => { try { rmi.loadModelManifest('no-such'); return false; } catch { return true; } })());

  // M2：落库幂等
  const r1 = await rmi.ensureInstallRow(pool, { tenantId: T1, modelKey: 'bge-m3' });
  const r2 = await rmi.ensureInstallRow(pool, { tenantId: T1, modelKey: 'bge-m3' });
  ok('M2a 首建 UNINSTALLED+local-hash 基线', r1.created && r1.row.state === 'UNINSTALLED'
    && r1.row.active_provider === 'local-hash-v1');
  ok('M2b 重复 ensure 幂等（不重建不覆盖）', r2.created === false && r2.row.install_id === r1.row.install_id);
  ok('M2c 落库快照钉死 manifest（版本/哈希清单/来源）', r1.row.manifest_version === 'bge-m3-modelscope-v1'
    && (r1.row.expected_files ?? []).length === 4 && r1.row.revision === m.source.files_revision);

  // M3：状态机合法/非法迁移
  ok('M3a UNINSTALLED→DOWNLOADING 合法', (await rmi.transitionInstall(pool,
    { tenantId: T1, modelKey: 'bge-m3', from: 'UNINSTALLED', to: 'DOWNLOADING' })).ok);
  ok('M3b DOWNLOADING→ACTIVE 非法（拒绝跳 READY/ACTIVATE）', !((await rmi.transitionInstall(pool,
    { tenantId: T1, modelKey: 'bge-m3', from: 'DOWNLOADING', to: 'ACTIVE' })).ok));
  {
    const r = await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3',
      from: 'DOWNLOADING', to: 'DOWNLOAD_FAILED', errorCode: 'network_reset' });
    const row = await rmi.getInstall(pool, { tenantId: T1, modelKey: 'bge-m3' });
    ok('M3c 失败态必须带错误码（DOWNLOAD_FAILED 落 last_error_code）',
      r.ok && row.last_error_code === 'network_reset', row.last_error_code);
  }
  {
    await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'DOWNLOAD_FAILED', to: 'DOWNLOADING' });
    await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'DOWNLOADING', to: 'VERIFYING' });
    await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'VERIFYING', to: 'HASH_MISMATCH', errorCode: 'sha256_mismatch:pytorch_model.bin' });
    const bad = await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'HASH_MISMATCH', to: 'READY' });
    ok('M3d HASH_MISMATCH→READY 非法（哈希不匹配不可激活）', !bad.ok, bad);
  }

  // M4：CAS 并发单赢家（UNINSTALLED 重置后两路并发 DOWNLOADING）
  await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'HASH_MISMATCH', to: 'UNINSTALLED' });
  await rmi.ensureInstallRow(pool, { tenantId: T1, modelKey: 'bge-m3' });
  const [c1, c2] = await Promise.all([
    rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'UNINSTALLED', to: 'DOWNLOADING' }),
    rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'UNINSTALLED', to: 'DOWNLOADING' }),
  ]);
  ok('M4a 并发迁移恰一赢家', c1.ok !== c2.ok);

  // M5：激活/回退 provider 切换（幂等）
  await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'DOWNLOADING', to: 'VERIFYING' });
  await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'VERIFYING', to: 'READY' });
  const act = await rmi.setProviderActive(pool, { tenantId: T1, modelKey: 'bge-m3', provider: 'bge-m3' });
  const row1 = await rmi.getInstall(pool, { tenantId: T1, modelKey: 'bge-m3' });
  ok('M5a 激活=state ACTIVE+provider bge-m3+activated_at', act.ok && !act.idempotent
    && row1.state === 'ACTIVE' && row1.active_provider === 'bge-m3' && !!row1.activated_at);
  const back = await rmi.setProviderActive(pool, { tenantId: T1, modelKey: 'bge-m3', provider: 'local-hash-v1' });
  const row2 = await rmi.getInstall(pool, { tenantId: T1, modelKey: 'bge-m3' });
  ok('M5b 回退=state READY+provider local-hash（安装保留可重激活）', back.ok
    && row2.state === 'READY' && row2.active_provider === 'local-hash-v1' && row2.activated_at !== null);
  const again = await rmi.setProviderActive(pool, { tenantId: T1, modelKey: 'bge-m3', provider: 'local-hash-v1' });
  ok('M5c 重复回退幂等', again.ok && again.idempotent);

  // M6：进度心跳仅在 DOWNLOADING 态生效
  const bumpIdle = await rmi.bumpProgress(pool, { tenantId: T1, modelKey: 'bge-m3', downloadedBytes: 100 });
  ok('M6a 非 DOWNLOADING 态心跳被拒（null）', bumpIdle === null);
  await rmi.transitionInstall(pool, { tenantId: T1, modelKey: 'bge-m3', from: 'READY', to: 'DOWNLOADING' });
  const bump = await rmi.bumpProgress(pool, { tenantId: T1, modelKey: 'bge-m3', downloadedBytes: 12345 });
  ok('M6b DOWNLOADING 态心跳生效', Number(bump) === 12345);

  // M7：tenant 隔离（第二租户独立命名空间）
  const T2 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('rmi-t2','RMI T2') RETURNING tenant_id`)).rows[0].tenant_id;
  const t2row = await rmi.ensureInstallRow(pool, { tenantId: T2, modelKey: 'bge-m3' });
  ok('M7 双租户独立行（T2 从 UNINSTALLED 起）', t2row.created && t2row.row.state === 'UNINSTALLED'
    && t2row.row.install_id !== r1.row.install_id);
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
