#!/usr/bin/env node
// console/backend/test/mu-skill-seed.integration.mjs — scripts/seed-skills.mjs 铺底测试：
// fresh PG（v17）→ 种子 → 双租户断言 → 重跑幂等（零新建/零新审计）。
// 运行：node console/backend/test/mu-skill-seed.integration.mjs（自起一次性 PG）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '../../..');
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-seed-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17600 + Math.floor(Math.random() * 60);
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
// 第二租户（验证铺底覆盖全部租户）
await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('seed-t2','Seed T2')`);

const runSeed = () => execFileSync('node', [path.join(ROOT, 'scripts/seed-skills.mjs')],
  { env: { ...process.env, CONSOLE_PG_DSN: dsn }, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] });

try {
  const out1 = runSeed();
  ok('SD1a 首跑零异常且输出计数', out1.includes('注册=12') && out1.includes('发布=12'), out1.trim().split('\n').pop());
  const counts = (await pool.query(`
    SELECT (SELECT count(*)::int FROM mu.skill) skills,
           (SELECT count(*)::int FROM mu.skill_version) versions,
           (SELECT count(*)::int FROM mu.skill WHERE current_version='1.0.0') activated`)).rows[0];
  ok('SD1b 6 技能 × 2 租户 = 12 行/12 版本', counts.skills === 12 && counts.versions === 12, counts);
  ok('SD1c 首版全部自动激活', counts.activated === 12, counts.activated);
  const keys = (await pool.query(`SELECT DISTINCT skill_key FROM mu.skill ORDER BY 1`)).rows.map((r) => r.skill_key);
  ok('SD1d 六技能键齐全（含 rag.retrieve）', JSON.stringify(keys) === JSON.stringify(
    ['rag.retrieve', 'skill_case_retrieval', 'skill_diff_parse', 'skill_risk_classify', 'skill_sast_scan', 'skill_test_runner']), keys);
  const audits1 = (await pool.query(`SELECT count(*)::int c FROM mu.audit_event WHERE kind LIKE 'MU_SKILL%'`)).rows[0].c;
  ok('SD1e 审计 24 事件（12 注册+12 发布）', audits1 === 24, audits1);

  const out2 = runSeed();
  ok('SD2a 重跑全跳过（幂等）', out2.includes('注册=0') && out2.includes('发布=0'), out2.trim().split('\n').pop());
  const audits2 = (await pool.query(`SELECT count(*)::int c FROM mu.audit_event WHERE kind LIKE 'MU_SKILL%'`)).rows[0].c;
  ok('SD2b 重跑零新增审计', audits2 === 24, audits2);
  const rows = (await pool.query(`SELECT (SELECT count(*)::int FROM mu.skill) s, (SELECT count(*)::int FROM mu.skill_version) v`)).rows[0];
  ok('SD2c 重跑零新增行', rows.s === 12 && rows.v === 12, rows);

  // 指纹与工件引用（诚实口径钉死）
  const rag = (await pool.query(`
    SELECT v.manifest_sha256, v.artifact_ref FROM mu.skill_version v
      JOIN mu.skill s USING(skill_id) WHERE s.skill_key='rag.retrieve' LIMIT 1`)).rows[0];
  ok('SD3 rag.retrieve 指纹=ragtrial 实现工件（b46add4 钉死）',
    /^[0-9a-f]{64}$/.test(rag.manifest_sha256) && rag.artifact_ref.includes('ragtrial') && rag.artifact_ref.includes('b46add4'), rag.artifact_ref);
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
