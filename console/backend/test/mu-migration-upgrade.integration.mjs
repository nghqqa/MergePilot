// console/backend/test/mu-migration-upgrade.integration.mjs — ADR-002 发布前验证：
// existing DB 升级路径（v16 → v17 增量迁移）+ initSchema 幂等（restart 安全）。
// v17=技能版本治理面（mu.skill/mu.skill_version，纯 additive）；历史断言保留
// v15/v16 特征面（本套件自 v15 形状播种一路升到 v17——逐版本链完整性一并覆盖）。
// 模拟既有库：先只跑 migration ≤16 + 播种 v1 形状数据与 v16 审批票，再走完整 initSchema。
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-mig-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17420 + Math.floor(Math.random() * 40);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;

const { MU_MIGRATIONS, MU_SCHEMA_LATEST } = await import('../lib/multiuser/schema.mjs');
const LAST = MU_MIGRATIONS[MU_MIGRATIONS.length - 1];
const PRIOR = MU_MIGRATIONS[MU_MIGRATIONS.length - 2];
if (Number(LAST.version) !== 17) {
  console.error(`前提漂移：最新迁移=${LAST.version}（本测试钉 v17 升级路径）`);
  process.exit(2);
}

async function applyUpTo(maxVer) {
  await pool.query(`CREATE SCHEMA IF NOT EXISTS mu`);
  await pool.query(`CREATE TABLE IF NOT EXISTS mu.schema_migrations (
    version INT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
  for (const m of MU_MIGRATIONS) {
    if (Number(m.version) > maxVer) break;
    const r = await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=$1`, [m.version]);
    if (r.rowCount) continue;
    for (const sql of m.sql) await pool.query(sql);
    await pool.query(`INSERT INTO mu.schema_migrations (version, name) VALUES ($1,$2)`, [m.version, m.name]);
  }
}

try {
  // ── 阶段 1：既有库（只到 v16）+ 播种 v1 形状数据与 v16 审批票 ──
  await applyUpTo(16);
  ok('U1 前置=迁移到 v16（v17 未应用）',
    (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=17`)).rowCount === 0);
  const seed = await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('mig','Mig') RETURNING tenant_id`);
  const T = seed.rows[0].tenant_id;
  const U = (await pool.query(`INSERT INTO mu.app_user (login, display_name) VALUES ('mig-u','Mig U') RETURNING user_id`)).rows[0].user_id;
  await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,'maintainer')`, [T, U]);
  const repo = (await pool.query(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
    VALUES ($1,'github','mig-r','mig','r') RETURNING repo_id`, [T])).rows[0];
  const pr = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,99,$3) RETURNING pr_id`, [T, repo.repo_id, crypto.randomBytes(20).toString('hex')])).rows[0];
  const v1run = (await pool.query(`INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
    VALUES ($1,$2,$3,$4,'COMPLETED') RETURNING run_id`, [T, repo.repo_id, pr.pr_id, 'aa'.repeat(20)])).rows[0];
  // v1 形状 agent_attempt（provider 枚举旧域）+ fix_attempt（旧 status 域）
  const att = (await pool.query(`INSERT INTO mu.agent_attempt (run_id, agent_role, attempt, provider,
    tenant_id, repo_id, pr_id, head_sha) VALUES ($1,'reviewer',1,'deterministic',$2,$3,$4,$5) RETURNING attempt_id`,
    [v1run.run_id, T, repo.repo_id, pr.pr_id, 'aa'.repeat(20)])).rows[0];
  const fix = (await pool.query(`INSERT INTO mu.fix_attempt (run_id, tenant_id, repo_id, pr_id, head_sha,
    attempt, status) VALUES ($1,$2,$3,$4,$5,1,'DRY_RUN') RETURNING fix_id`,
    [v1run.run_id, T, repo.repo_id, pr.pr_id, 'aa'.repeat(20)])).rows[0];
  const ver = (await pool.query(`INSERT INTO mu.verification_attempt (run_id, fix_id, tenant_id, repo_id,
    pr_id, head_sha, attempt, verdict) VALUES ($1,$2,$3,$4,$5,$6,1,'PASS') RETURNING verify_id`,
    [v1run.run_id, fix.fix_id, T, repo.repo_id, pr.pr_id, 'aa'.repeat(20)])).rows[0];
  ok('U2 v1 形状数据播种（run/attempt/fix/verification）', Boolean(v1run && att && fix && ver));

  // ── 阶段 2：完整 initSchema（升级路径）──
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const store = await createMuStore({ pool });
  await store.initSchema();
  const v17 = (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=17`)).rowCount;
  ok('U3 升级后 v17 应用', v17 === 1);
  ok('U3b 版本=最新', Number(MU_SCHEMA_LATEST) === 17);
  for (const t of ['review_policy', 'review_policy_revision', 'provider_registry',
    'provider_consent', 'code_egress_event']) {
    const has = (await pool.query(`SELECT 1 FROM information_schema.tables
      WHERE table_schema='mu' AND table_name=$1`, [t])).rowCount;
    ok(`U3c v15 新表在位：${t}`, has === 1);
  }
  const col = (await pool.query(`SELECT column_name FROM information_schema.columns
    WHERE table_schema='mu' AND table_name='review_run' AND column_name='architecture_version'`)).rowCount;
  ok('U3d review_run 增列（architecture_version 等 14 列之一）', col === 1);
  const faT = (await pool.query(`SELECT 1 FROM information_schema.tables
    WHERE table_schema='mu' AND table_name='fix_approval'`)).rowCount;
  ok('U3e v16 新表 mu.fix_approval 在位', faT === 1);

  // 旧数据零丢失
  const kept = (await pool.query(`
    SELECT (SELECT count(*)::int FROM mu.tenant WHERE tenant_id=$1)
        + (SELECT count(*)::int FROM mu.review_run WHERE run_id=$2 AND status='COMPLETED')
        + (SELECT count(*)::int FROM mu.fix_attempt WHERE fix_id=$3 AND status='DRY_RUN')
        + (SELECT count(*)::int FROM mu.verification_attempt WHERE verify_id=$4 AND verdict='PASS') AS kept`,
    [T, v1run.run_id, fix.fix_id, ver.verify_id])).rows[0].kept;
  ok('U4 既有 v1 数据升级后零丢失（4/4 原样）', Number(kept) === 4, { kept });
  const legacyNull = (await pool.query(
    `SELECT architecture_version, review_verdict FROM mu.review_run WHERE run_id=$1`, [v1run.run_id])).rows[0];
  ok('U4b legacy run 增列=NULL（不冒充 v2）',
    legacyNull.architecture_version === null && legacyNull.review_verdict === null);

  // 约束换新（升级后新域可用）
  const staleOk = await pool.query(
    `UPDATE mu.fix_attempt SET status='STALE' WHERE fix_id=$1 RETURNING fix_id`, [fix.fix_id]);
  ok('U5a fix_attempt 新域 STALE 可写（约束已换）', staleOk.rowCount === 1);
  const incOk = await pool.query(
    `INSERT INTO mu.verification_attempt (run_id, fix_id, tenant_id, repo_id, pr_id, head_sha, attempt, verdict)
     VALUES ($1,$2,$3,$4,$5,$6,2,'INCONCLUSIVE') RETURNING verify_id`,
    [v1run.run_id, fix.fix_id, T, repo.repo_id, pr.pr_id, 'aa'.repeat(20)]);
  ok('U5b verification_attempt 新域 INCONCLUSIVE 可写', incOk.rowCount === 1);
  const extAtt = await pool.query(
    `INSERT INTO mu.agent_attempt (run_id, agent_role, attempt, provider, tenant_id, repo_id, pr_id, head_sha)
     VALUES ($1,'reviewer',2,'external_api',$2,$3,$4,$5) RETURNING attempt_id`,
    [v1run.run_id, T, repo.repo_id, pr.pr_id, 'aa'.repeat(20)]);
  ok('U5c agent_attempt 新域 external_api 可写', extAtt.rowCount === 1);
  // v16 约束换新：review_run 新状态可写 + 活票唯一索引在位
  const waitRun = await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
     VALUES ($1,$2,$3,$4,'WAITING_FOR_HUMAN_APPROVAL') RETURNING run_id`,
    [T, repo.repo_id, pr.pr_id, 'bb'.repeat(20)]);
  ok('U5d v16 review_run 新态 WAITING_FOR_HUMAN_APPROVAL 可写（CHECK 已换）', waitRun.rowCount === 1);
  const faIdx = (await pool.query(`SELECT 1 FROM pg_indexes
    WHERE schemaname='mu' AND indexname='mu_fix_approval_live_uk'`)).rowCount;
  ok('U5e 活票唯一索引在位', faIdx === 1);

  // ── 阶段 2b：v17 技能治理面在位 + 播种技能数据（验证 restart 后零丢失）──
  for (const t of ['skill', 'skill_version']) {
    const has = (await pool.query(`SELECT 1 FROM information_schema.tables
      WHERE table_schema='mu' AND table_name=$1`, [t])).rowCount;
    ok(`U3f v17 新表 mu.${t} 在位`, has === 1);
  }
  const sk = (await pool.query(`INSERT INTO mu.skill (tenant_id, skill_key, display_name, created_by)
    VALUES ($1,'rag.retrieve','检索技能',$2) RETURNING skill_id`, [T, U])).rows[0];
  const skv = (await pool.query(`INSERT INTO mu.skill_version (tenant_id, skill_id, version,
    changelog, manifest_sha256, created_by) VALUES ($1,$2,'1.0.0','init','${'ab'.repeat(32)}',$3) RETURNING version_id`,
    [T, sk.skill_id, U])).rows[0];
  const dupVKey = await pool.query(
    `INSERT INTO mu.skill_version (tenant_id, skill_id, version, changelog, manifest_sha256, created_by)
     VALUES ($1,$2,'1.0.0','dup','${'cd'.repeat(32)}',$3)`, [T, sk.skill_id, U]).then(() => false, () => true);
  ok('U5f v17 唯一约束：同 skill 同版本号拒写', dupVKey === true);

  // ── 阶段 3：initSchema 重放（restart 安全/幂等）──
  await store.initSchema();
  ok('U6 initSchema 重放幂等（restart 安全——零异常）', true);
  const dupVer = (await pool.query(
    `SELECT count(*)::int FROM mu.schema_migrations WHERE version=17`)).rows[0].count;
  ok('U6b v17 不重复应用', Number(dupVer) === 1);
  const skKept = (await pool.query(`
    SELECT (SELECT count(*)::int FROM mu.skill WHERE skill_id=$1)
         + (SELECT count(*)::int FROM mu.skill_version WHERE version_id=$2) AS kept`,
    [sk.skill_id, skv.version_id])).rows[0].kept;
  ok('U6d 重放后技能/版本数据零丢失（2/2 原样）', Number(skKept) === 2, { skKept });
  await store.bootstrap();
  ok('U6c bootstrap 重放幂等', true);
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
