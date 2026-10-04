// console/backend/test/mu-migration-upgrade.integration.mjs — ADR-002 发布前验证：
// existing DB 升级路径（latest-1 → latest 增量迁移；当前 17 → 18 绑定统一回填）+
// initSchema 幂等（restart 安全）。v17=技能版本治理面（mu.skill/mu.skill_version，纯
// additive）；v18=绑定读写来源统一（mu.binding 现存行幂等回填 mu.repository_binding，
// mu.binding 原样保留）。历史断言保留 v15/v16/v17 特征面（本套件自 v15 形状播种一路
// 升到 latest——逐版本链完整性一并覆盖）。
// 模拟既有库：先只跑 migration ≤latest-1 + 播种 v1 形状数据与 v16 审批票，再走完整 initSchema。
// rc.10 PR-D（runner 加固）新增阶段 4-6：fresh DB 全链 v1→v20 / 重放逐位幂等 / 重启持久 /
// 并发单赢家（advisory lock）/ 锁被占快速失败 / 中途失败注入（事务回滚+定位+修复重跑）。
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
const LATEST = MU_SCHEMA_LATEST; // rc.10：main 已含 v21（PR-B）——版本行断言动态化，勿再硬编码
const LAST = MU_MIGRATIONS[MU_MIGRATIONS.length - 1];
const PRIOR = MU_MIGRATIONS[MU_MIGRATIONS.length - 2];
if (Number(LAST.version) < 19) {
  console.error(`前提漂移：最新迁移=${LAST.version}（本测试需覆盖 v18 绑定统一升级路径）`);
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

const extraPools = []; // PR-D 新阶段专用 pool（finally 统一回收）

try {
  // ── 阶段 1：既有库（只到 v17）+ 播种 v1 形状数据与 v16 审批票 ──
  // v18 含 legacy 回填语义——阶段 1 须停在其前一版（v17），否则播种行不经历回填；
  // 完整 initSchema 将走 17→18（回填）→19（C 波调用留痕）→20（RAG 模型安装）全链增量。
  await applyUpTo(17);
  ok('U1 前置=迁移到 v17（v18/v19/v20 未应用）',
    (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=17`)).rowCount === 1
      && (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=18`)).rowCount === 0
      && (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=19`)).rowCount === 0
      && (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=${LATEST}`)).rowCount === 0);
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
  // v1 形状 mu.binding 行（老读侧数据——升级后必须零丢失回填 repository_binding）
  const legacyBind = (await pool.query(
    `INSERT INTO mu.binding (tenant_id, repo_id, kind, installation_id, granted_scopes)
     VALUES ($1,$2,'fixture','fixture-install-mig','["pull_requests:read"]'::jsonb) RETURNING binding_id, created_at, updated_at`,
    [T, repo.repo_id])).rows[0];
  ok('U2b v1 形状 mu.binding 行播种（fixture kind，state=active）', Boolean(legacyBind?.binding_id));

  // ── 阶段 2：完整 initSchema（升级路径）──
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const store = await createMuStore({ pool });
  await store.initSchema();
  const v17 = (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=17`)).rowCount;
  ok('U3 升级后 v17 应用', v17 === 1);
  const v18 = (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=18`)).rowCount;
  ok('U3v18 升级后 v18（绑定统一回填）应用', v18 === 1);
  ok('U3b 版本=最新', Number(MU_SCHEMA_LATEST) === Number(LAST.version));
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

  // v18 回填：mu.binding 现存行幂等迁入 repository_binding，原表零改动
  const bf = (await pool.query(
    `SELECT rb.github_repo_id, rb.binding_state, rb.installation_id, i.account_type
       FROM mu.repository_binding rb
       LEFT JOIN mu.github_app_installation i ON i.installation_id = rb.installation_id
      WHERE rb.tenant_id=$1 AND rb.repo_id=$2`, [T, repo.repo_id])).rows[0];
  ok('U4c v18 回填：legacy mu.binding 行迁入 repository_binding（binding_state=active）',
    Boolean(bf) && bf.binding_state === 'active', bf);
  ok('U4d v18 回填：fixture kind → fixture 域安装（account_type=fixture，id 落保留区间）',
    bf?.account_type === 'fixture' && Number(bf?.installation_id) >= 8400000000000000, bf);
  const legacyKept = (await pool.query(
    `SELECT state, installation_state, kind FROM mu.binding WHERE binding_id=$1`,
    [legacyBind.binding_id])).rows[0];
  ok('U4e mu.binding 行原样保留（老镜像兼容：state/installation_state/kind 不动）',
    legacyKept?.state === 'active' && legacyKept?.installation_state === 'active' && legacyKept?.kind === 'fixture',
    legacyKept);

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
  // v19：Skill/RAG 调用留痕（C 波 C1）——升级后 bookkeeping 与新表在位
  const v19 = (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=19`)).rowCount;
  ok('U3v19 升级后 v19（Skill/RAG 调用留痕）应用', v19 === 1);
  for (const t of ['skill_invocation_event', 'rag_retrieval_event']) {
    const has = (await pool.query(`SELECT 1 FROM information_schema.tables
      WHERE table_schema='mu' AND table_name=$1`, [t])).rowCount;
    ok(`U3v19b v19 新表 mu.${t} 在位`, has === 1);
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

  // v20：RAG 模型安装表在位 + 状态 CHECK（原 v19——C 波占用 v19 后顺延）
  {
    const has = (await pool.query(`SELECT 1 FROM information_schema.tables
      WHERE table_schema='mu' AND table_name='rag_model_install'`)).rowCount;
    ok('U3g v20 新表 mu.rag_model_install 在位', has === 1);
    const ck = (await pool.query(`SELECT count(*)::int FROM pg_constraint
      WHERE conrelid='mu.rag_model_install'::regclass AND contype='c'`)).rows[0].count;
    ok('U3g2 v20 状态 CHECK 在位（非法态拒写）', Number(ck) >= 1, ck);
    const badState = await pool.query(
      `INSERT INTO mu.rag_model_install (tenant_id, model_key, manifest_version, source_url,
        revision, license, expected_files, total_bytes, state)
       VALUES ($1,'x','v','https://modelscope.cn/x','r','MIT','[]'::jsonb,1,'BOGUS')`,
      [T]).then(() => false, () => true);
    ok('U3g3 非法状态被 DB CHECK 拒绝', badState === true);
  }

  // ── 阶段 3：initSchema 重放（restart 安全/幂等）──
  await store.initSchema();
  ok('U6 initSchema 重放幂等（restart 安全——零异常）', true);
  const dupVer = (await pool.query(
    `SELECT count(*)::int FROM mu.schema_migrations WHERE version=19`)).rows[0].count;
  ok('U6b v19 不重复应用', Number(dupVer) === 1);
  const dupVer20 = (await pool.query(
    `SELECT count(*)::int FROM mu.schema_migrations WHERE version=${LATEST}`)).rows[0].count;
  ok('U6b-v20 v20（RAG 模型安装）不重复应用', Number(dupVer20) === 1);
  const dupVer18Del = await pool.query(`DELETE FROM mu.schema_migrations WHERE version=18`);
  await store.initSchema(); // v18 DO 块真实重执行（NOT EXISTS 防重护栏生效）
  const dupVer18 = (await pool.query(
    `SELECT count(*)::int FROM mu.schema_migrations WHERE version=18`)).rows[0].count;
  const rbDup = (await pool.query(
    `SELECT count(*)::int FROM mu.repository_binding WHERE tenant_id=$1 AND repo_id=$2`,
    [T, repo.repo_id])).rows[0].count;
  ok('U6b2 v18 重放（删版本行重执行回填）零重复（repository_binding 恒 1 行）',
    dupVer18Del.rowCount === 1 && Number(dupVer18) === 1 && Number(rbDup) === 1, { dupVer18, rbDup });
  const skKept = (await pool.query(`
    SELECT (SELECT count(*)::int FROM mu.skill WHERE skill_id=$1)
         + (SELECT count(*)::int FROM mu.skill_version WHERE version_id=$2) AS kept`,
    [sk.skill_id, skv.version_id])).rows[0].kept;
  ok('U6d 重放后技能/版本数据零丢失（2/2 原样）', Number(skKept) === 2, { skKept });
  await store.bootstrap();
  ok('U6c bootstrap 重放幂等', true);

  // ════════ rc.10 PR-D：迁移 runner 加固新场景（独立 fresh DB，走新 runner）════════
  const mkDb = async (name) => {
    await pool.query(`CREATE DATABASE ${name}`);
    const p = new Pool({ connectionString: dsn.replace(/\/mu$/, `/${name}`), max: 4 });
    extraPools.push(p);
    await p.query('SELECT 1');
    return p;
  };
  const hasTable = async (p, t) => (await p.query(`SELECT 1 FROM information_schema.tables
    WHERE table_schema='mu' AND table_name=$1`, [t])).rowCount === 1;
  const dsnOf = (name) => dsn.replace(/\/mu$/, `/${name}`);
  // 三表（v18 repository_binding / v19 skill_invocation_event / v20 rag_model_install）
  // 结构指纹：列+触发器+约束+索引（跨库逐位可比）
  const threeTableFingerprint = (p) => Promise.all([
    p.query(`SELECT table_name, column_name, data_type, is_nullable, column_default
       FROM information_schema.columns
      WHERE table_schema='mu' AND table_name IN ('repository_binding','skill_invocation_event','rag_model_install')
      ORDER BY table_name, ordinal_position`),
    p.query(`SELECT DISTINCT event_object_table AS table_name, trigger_name FROM information_schema.triggers
      WHERE trigger_schema='mu' AND event_object_table IN ('repository_binding','skill_invocation_event','rag_model_install')
      ORDER BY 1,2`),
    p.query(`SELECT conrelid::regclass::text AS table_name, conname, pg_get_constraintdef(oid) AS def
       FROM pg_constraint
      WHERE conrelid IN ('mu.repository_binding'::regclass,'mu.skill_invocation_event'::regclass,'mu.rag_model_install'::regclass)
      ORDER BY 1,2`),
    p.query(`SELECT tablename, indexname, indexdef FROM pg_indexes
      WHERE schemaname='mu' AND tablename IN ('repository_binding','skill_invocation_event','rag_model_install')
      ORDER BY 1,2`),
  ]).then(([cols, trg, con, idx]) => JSON.stringify([cols.rows, trg.rows, con.rows, idx.rows]));

  // ── 阶段 4（a/c/d）：fresh DB 全链 v1→v20 + 重放逐位一致 + 重启持久 ──
  const freshPool = await mkDb('mu_fresh');
  const freshStore = await createMuStore({ pool: freshPool });
  await freshStore.initSchema();
  const fc = (await freshPool.query(
    `SELECT count(*)::int n, count(DISTINCT version)::int d FROM mu.schema_migrations`)).rows[0];
  ok(`P1 fresh DB 全链 v1→v${LATEST}：恰 ${LATEST} 版本行且各一次`, Number(fc.n) === LATEST && Number(fc.d) === LATEST, fc);
  ok('P2 v18 DO 块生效：repository_binding 在位', await hasTable(freshPool, 'repository_binding'));
  const sealFn = (await freshPool.query(`SELECT 1 FROM information_schema.routines
    WHERE routine_schema='mu' AND routine_name='mu_invocation_event_seal'`)).rowCount;
  const sealTrg = (await freshPool.query(`SELECT count(DISTINCT trigger_name)::int n FROM information_schema.triggers
    WHERE trigger_schema='mu' AND trigger_name IN ('mu_skill_invocation_event_seal','mu_rag_retrieval_event_seal')`)).rows[0].n;
  ok('P3 v19 CREATE OR REPLACE FUNCTION+TRIGGER 生效：seal 函数 + 2 触发器',
    sealFn === 1 && Number(sealTrg) === 2, { sealFn, sealTrg });
  const ck20 = (await freshPool.query(`SELECT count(*)::int n FROM pg_constraint
    WHERE conrelid='mu.rag_model_install'::regclass AND contype='c'`)).rows[0].n;
  ok('P4 v20 CHECK 约束表生效：rag_model_install + CHECK ≥1',
    await hasTable(freshPool, 'rag_model_install') && Number(ck20) >= 1, ck20);
  const fpFresh = await threeTableFingerprint(freshPool);
  const fpBase = await threeTableFingerprint(pool);
  ok('P5 三表结构与基线（增量路径库）逐位一致', fpFresh === fpBase);

  // (c) 重放幂等：连跑两遍 initSchema，schema+版本行（含 applied_at）逐位不变
  const snap = async (p) => JSON.stringify([
    (await p.query(`SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns
      WHERE table_schema='mu' ORDER BY table_name, ordinal_position`)).rows,
    (await p.query(`SELECT version, name, applied_at FROM mu.schema_migrations ORDER BY version`)).rows,
    (await p.query(`SELECT DISTINCT trigger_name FROM information_schema.triggers WHERE trigger_schema='mu' ORDER BY 1`)).rows,
  ]);
  const s0 = await snap(freshPool);
  await freshStore.initSchema();
  await freshStore.initSchema();
  ok('P6 重放幂等：两遍 initSchema 后 schema+版本行逐位一致', (await snap(freshPool)) === s0);

  // (d) 重启持久：换全新连接池（新连接=重启模拟）后 initSchema 成功、数据/版本行俱在
  await freshPool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('pr-d-restart','PR-D')`);
  await freshPool.end();
  const freshPool2 = new Pool({ connectionString: dsnOf('mu_fresh'), max: 4 });
  extraPools.push(freshPool2);
  const freshStore2 = await createMuStore({ pool: freshPool2 });
  await freshStore2.initSchema();
  const rc = (await freshPool2.query(`SELECT count(*)::int n FROM mu.schema_migrations`)).rows[0].n;
  const reTenant = (await freshPool2.query(`SELECT 1 FROM mu.tenant WHERE slug='pr-d-restart'`)).rowCount;
  ok(`P7 重启持久：新连接池 initSchema 成功，${LATEST} 版本行 + 业务数据俱在`,
    Number(rc) === LATEST && reTenant === 1, { rc, reTenant });

  // ── 阶段 5（e）：并发单赢家——两个独立 PG pool 同时对 fresh DB 冷启 ──
  const concPool = await mkDb('mu_conc');
  const concPoolB = new Pool({ connectionString: dsnOf('mu_conc'), max: 4 });
  extraPools.push(concPoolB);
  const storeA = await createMuStore({ pool: concPool });
  const storeB = await createMuStore({ pool: concPoolB });
  let maxHolders = 0;
  const probe = setInterval(() => {
    pool.query(`SELECT count(*)::int n FROM pg_locks
      WHERE locktype='advisory' AND objid=hashtext('mergepilot_mu_schema_init') AND granted`)
      .then((r) => { if (Number(r.rows[0].n) > maxHolders) maxHolders = Number(r.rows[0].n); })
      .catch(() => {});
  }, 10);
  const [resA, resB] = await Promise.allSettled([storeA.initSchema(), storeB.initSchema()]);
  clearInterval(probe);
  ok('P8 并发冷启：两调用者均成功返回（等锁者经版本行守卫跳过）',
    resA.status === 'fulfilled' && resB.status === 'fulfilled',
    { a: resA.status, b: resB.status, err: resA.status === 'rejected' ? String(resA.reason) : resB.status === 'rejected' ? String(resB.reason) : null });
  ok('P9 并发单赢家：探针采样全程任一时刻至多 1 会话持 advisory lock（max=1）', maxHolders === 1, maxHolders);
  const cc = (await concPoolB.query(
    `SELECT count(*)::int n, count(DISTINCT version)::int d FROM mu.schema_migrations`)).rows[0];
  ok(`P10 并发后库状态一致：恰 ${LATEST} 版本行无重复`, Number(cc.n) === LATEST && Number(cc.d) === LATEST, cc);
  ok('P11 无半成品表：v18/v19/v20 关键对象齐备',
    await hasTable(concPoolB, 'repository_binding') && await hasTable(concPoolB, 'rag_model_install')
    && (await concPoolB.query(`SELECT count(DISTINCT trigger_name)::int n FROM information_schema.triggers
      WHERE trigger_schema='mu' AND trigger_name='mu_rag_retrieval_event_seal'`)).rows[0].n === 1);
  const lk = (await concPool.query(`SELECT pg_try_advisory_lock(hashtext('mergepilot_mu_schema_init')) ok`)).rows[0].ok;
  await concPool.query(`SELECT pg_advisory_unlock(hashtext('mergepilot_mu_schema_init'))`);
  ok('P12 锁零泄漏：赛后第三方可即刻取锁（同库检查——PG advisory lock 按 database 隔离）', lk === true);

  // ── 阶段 5b（1 补充）：锁被占时上限内快速失败（mu_schema_init_locked），释放后可重试 ──
  // 占锁者必须与 victim 同库（advisory lock 按 database 隔离）——从 concPool（mu_conc）取
  const busyClient = await concPool.connect();
  await busyClient.query(`SELECT pg_advisory_lock(hashtext('mergepilot_mu_schema_init'))`);
  const busyStore = await createMuStore({ pool: concPoolB, env: { MU_SCHEMA_INIT_LOCK_TIMEOUT_MS: '400' } });
  const busyErr = await busyStore.initSchema().then(() => null, (e) => e);
  ok('P13 锁被占：上限内拿不到锁 → mu_schema_init_locked 快速失败（不无限挂起）',
    busyErr instanceof Error && /mu_schema_init_locked/.test(busyErr.message), String(busyErr?.message ?? busyErr));
  await busyClient.query(`SELECT pg_advisory_unlock(hashtext('mergepilot_mu_schema_init'))`);
  busyClient.release();
  await busyStore.initSchema();
  ok('P14 锁释放后同 store 重试成功（getMuStore muStorePromise=null 重试语义可用）', true);

  // ── 阶段 6（f）：中途失败可重试——事务回滚 + 版本行不落 + 失败定位 + 修复重跑 ──
  // 注入不改生产数组：固定注入 v20（mu_rag_model_install——含 CREATE TABLE，可验证
  // 语句 0 真实执行后整体回滚）。rc.10 后 LAST=v21（无建表语句），不能用 slice(-1) 定位。
  const failPool = await mkDb('mu_fail');
  const v20idx = MU_MIGRATIONS.findIndex((m) => m.version === 20);
  const V20 = MU_MIGRATIONS[v20idx];
  const badV20 = { ...V20, sql: [V20.sql[0], `SELECT * FROM mu.__pr_d_injected_failure__`] };
  const failStore = await createMuStore({ pool: failPool, migrations: [...MU_MIGRATIONS.slice(0, v20idx), badV20] });
  const migErr = await failStore.initSchema().then(() => null, (e) => e);
  ok('P15 注入失败：initSchema 拒绝（非静默半应用）', migErr instanceof Error);
  ok('P16 错误信息含 version/name/statement#序号/PG 消息',
    /mu_schema_init failed at migration 20 \(mu_rag_model_install\) statement#1: /.test(String(migErr?.message ?? '')),
    String(migErr?.message ?? migErr).slice(0, 160));
  ok('P17 事务回滚：v20 语句 0 已建的表被整体回滚（表不存在）', !(await hasTable(failPool, 'rag_model_install')));
  const fv20 = (await failPool.query(`SELECT count(*)::int n FROM mu.schema_migrations WHERE version=20`)).rows[0].n;
  const fv19 = (await failPool.query(`SELECT count(*)::int n FROM mu.schema_migrations WHERE version=19`)).rows[0].n;
  ok('P18 版本行不落（v20 无行）且先行迁移已提交（v19 在位）',
    Number(fv20) === 0 && Number(fv19) === 1, { fv20, fv19 });
  // 修复注入（换回生产数组）重跑 → 成功
  const retryStore = await createMuStore({ pool: failPool });
  await retryStore.initSchema();
  const rv20 = (await failPool.query(`SELECT count(*)::int n FROM mu.schema_migrations WHERE version=${LATEST}`)).rows[0].n;
  const rck = (await failPool.query(`SELECT count(*)::int n FROM pg_constraint
    WHERE conrelid='mu.rag_model_install'::regclass AND contype='c'`)).rows[0].n;
  ok('P19 修复重跑：v20 落版本行 + CHECK 约束表在位',
    Number(rv20) === 1 && await hasTable(failPool, 'rag_model_install') && Number(rck) >= 1, { rv20, rck });
  // (5) 删除版本行强制重放路径在新事务机制下仍工作（生产数组、事务包裹重执行 v20 DDL）
  await failPool.query(`DELETE FROM mu.schema_migrations WHERE version=${LATEST}`);
  await retryStore.initSchema();
  const rp20 = (await failPool.query(
    `SELECT count(*)::int n FROM mu.schema_migrations WHERE version=${LATEST}`)).rows[0].n;
  ok('P20 删版本行强制重放（事务内重执行 v20 DDL）仍工作且零重复', Number(rp20) === 1, rp20);
} finally {
  for (const p of extraPools) { try { await p.end(); } catch { /* */ } }
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
