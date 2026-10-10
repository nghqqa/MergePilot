// 迁移演练：v23（rc.22 基线）→ v25（Gitee 接入）——隔离库全序
// 场景：rc.22 的 schema.mjs（v1-v23）bootstrap → 种子旧数据（GitHub 绑定/PR/历史 run/
//       审批票/任务）→ 当前分支（v24/v25）迁移 → 断言旧数据/外键/审批/任务兼容。
// 用法：node scripts/dev/migration-drill-v23-v25.mjs （自起隔离 PG，结束清理）
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const backend = path.resolve(here, '../../console/backend');
const require2 = (await import('node:module')).createRequire(path.join(backend, 'test/support/noop.js'));
const { Pool } = require2('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-mig-drill-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16800 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const DSN = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const cleanup = () => { try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ } };
process.on('exit', cleanup);

try {
  for (let i = 0; i < 60; i++) {
    try { const p = new Pool({ connectionString: DSN }); await p.query('SELECT 1'); await p.end(); break; }
    catch { await new Promise((r) => setTimeout(r, 800)); }
  }

  // ── 步骤 1：rc.22 基线（v1-v23）bootstrap ──
  const v23mod = await import(`file://${path.join(backend, '.mig-drill-schema-v23.mjs').replace(/\\/g, '/')}`).catch(async () => {
    // 从 git tag 87f292e（rc.22）导出 v23 schema 到临时文件（不入库）
    const src = execFileSync('git', ['show', '87f292e:console/backend/lib/multiuser/schema.mjs'], { cwd: path.resolve(backend, '../..'), encoding: 'utf8' });
    const fs = (await import('node:fs')).default;
    fs.writeFileSync(path.join(backend, '.mig-drill-schema-v23.mjs'), src);
    return import(`file://${path.join(backend, '.mig-drill-schema-v23.mjs').replace(/\\/g, '/')}`);
  });
  const { createMuStore } = await import(`file://${path.join(backend, 'lib/multiuser/store.mjs').replace(/\\/g, '/')}`);
  const pool = new Pool({ connectionString: DSN });
  const st23 = await createMuStore({ pool, env: process.env, migrations: v23mod.MU_MIGRATIONS });
  await st23.initSchema();
  const v23top = Number((await pool.query('SELECT max(version) v FROM mu.schema_migrations')).rows[0].v);
  ok('S1 rc.22 基线 bootstrap 至 v23', v23top === 23, v23top);

  // ── 步骤 2：种子 rc.22 时代旧数据（生产形态代表样本）──
  const q = (t, p2) => pool.query(t, p2);
  const t1 = (await q(`INSERT INTO mu.tenant (slug, display_name) VALUES ('migdrill','演练租户') RETURNING tenant_id`)).rows[0].tenant_id;
  await q(`INSERT INTO mu.app_user (login) VALUES ('drill-maintainer')`);
  const u1 = (await q(`SELECT user_id FROM mu.app_user WHERE login='drill-maintainer'`)).rows[0].user_id;
  await q(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,'maintainer')`, [t1, u1]);
  await q(`INSERT INTO mu.github_app_installation (installation_id, tenant_id, app_id, account_id, account_login, account_type)
           VALUES (1,$1,1,901,'w31','User')`, [t1]);
  const r1 = (await q(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
           VALUES ($1,'github','98001','w31','r') RETURNING repo_id`, [t1])).rows[0];
  await q(`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, binding_state)
           VALUES ($1,$2,98001,'w31','r',1,'active')`, [t1, r1.repo_id]);
  const p1 = (await q(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
           VALUES ($1,$2,201,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','open') RETURNING pr_id`, [t1, r1.repo_id])).rows[0].pr_id;
  // 历史 run + 审批票（v16 审批门语义样本）
  const { createRunIfAbsent, claimNextAttempt, insertFindings } = await import(`file://${path.join(backend, 'lib/multiuser/orchestration.mjs').replace(/\\/g, '/')}`);
  const { run: run1 } = await createRunIfAbsent(pool, { tenantId: t1, repoId: r1.repo_id, prId: p1, headSha: 'a'.repeat(40) });
  const att = await claimNextAttempt(pool, { runId: run1.run_id, agentRole: 'reviewer', provider: 'deterministic',
    maxAttempts: 3, tenantId: t1, repoId: r1.repo_id, prId: p1, headSha: 'a'.repeat(40) });
  await insertFindings(pool, { attemptId: att.attemptId, runId: run1.run_id, tenantId: t1, repoId: r1.repo_id,
    prId: p1, headSha: 'a'.repeat(40),
    findings: [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 'src.js', line_start: 2,
      line_end: null, title: '凭据', evidence_ref: 'diff:src.js#L2', remediation: '轮换', summary_masked: 'masked' }] });
  const f1 = (await q(`SELECT finding_id FROM mu.agent_finding WHERE run_id=$1`, [run1.run_id])).rows[0].finding_id;
  const faMod = await import(`file://${path.join(backend, 'lib/multiuser/fix-approval.mjs').replace(/\\/g, '/')}`);
  await faMod.ensureFixApprovals(pool, { run: run1, binding: { tenantId: t1, repoId: r1.repo_id, prId: p1, headSha: 'a'.repeat(40) } });
  const faBefore = Number((await q(`SELECT count(*) c FROM mu.fix_approval WHERE run_id=$1`, [run1.run_id])).rows[0].c);
  const jobBefore = (await q(`INSERT INTO mu.job (tenant_id, repo_id, kind, requested_by, requested_role, payload)
           VALUES ($1,$2,'event_sync',NULL,'maintainer',$3::jsonb) RETURNING job_id`,
    [t1, r1.repo_id, JSON.stringify({ event: 'pull_request', action: 'synchronize', head_sha: 'a'.repeat(40),
      pr_number: 201, github_repo_id: 98001, installation_id: 1,
      delivery_id: 'drill-legacy-0000', trigger_source: 'manual' })])).rows[0].job_id;
  ok('S2 旧数据种子（绑定/PR/run/finding/审批票/任务）', faBefore >= 1 && Number(faBefore) >= 1);

  // ── 步骤 3：当前分支迁移（v24/v25 增量——runner 只跑未应用版本）──
  const st25 = await createMuStore({ pool, env: process.env });
  await st25.initSchema();
  const v25top = Number((await pool.query('SELECT max(version) v FROM mu.schema_migrations')).rows[0].v);
  ok('S3 增量迁移至 v25（不重放 v1-v23）', v25top === 25, v25top);
  const migCnt = Number((await q(`SELECT count(*) c FROM mu.schema_migrations`)).rows[0].c);
  ok('S3b 迁移行数=25（无重复重放）', migCnt === 25, migCnt);

  // ── 步骤 4：旧数据/外键/审批/任务兼容断言 ──
  const bindAfter = Number((await q(`SELECT count(*) c FROM mu.repository_binding WHERE binding_state='active' AND github_repo_id=98001`)).rows[0].c);
  ok('S4a GitHub 绑定完好（唯一约束未破坏）', bindAfter === 1);
  const prAfter = (await q(`SELECT pr_id, head_sha FROM mu.pull_request WHERE pr_id=$1`, [p1])).rows[0];
  ok('S4b 历史 PR 行完好', prAfter?.pr_id === p1 && prAfter?.head_sha === 'a'.repeat(40));
  const faAfter = Number((await q(`SELECT count(*) c FROM mu.fix_approval WHERE run_id=$1 AND status='PENDING'`, [run1.run_id])).rows[0].c);
  ok('S4c 历史审批票完好（外键级联无损）', faAfter === faBefore);
  const jobAfter = (await q(`SELECT state, payload->>'event' ev FROM mu.job WHERE job_id=$1`, [jobBefore])).rows[0];
  ok('S4d 旧任务保留且 payload 原样', jobAfter?.ev === 'pull_request');
  const runFrozen = (await q(`SELECT context_completeness, context_source FROM mu.review_run WHERE run_id=$1`, [run1.run_id])).rows[0];
  ok('S4e 历史 run 冻结列为 NULL（未记录≠伪造）', runFrozen?.context_completeness === null && runFrozen?.context_source === null);
  const repoCols = (await q(`SELECT forge_instance_id FROM mu.repository WHERE repo_id=$1`, [r1.repo_id])).rows[0];
  ok('S4f repository.forge_instance_id 回填 github-com（现读路径不引用）', repoCols?.forge_instance_id === 'github-com');
  // 新代码读路径：legacy GitHub 服务链解析照常
  const { resolveServiceContext } = await import(`file://${path.join(backend, 'lib/multiuser/review-service.mjs').replace(/\\/g, '/')}`);
  const svc = await resolveServiceContext(pool, { githubRepoId: 98001, prNumber: 201 });
  ok('S4g 新代码 legacy GitHub 服务链解析照常', svc?.ok === true && svc?.installationId === '1');
  // 幂等：重跑迁移=零变更
  const st25b = await createMuStore({ pool, env: process.env });
  await st25b.initSchema();
  const migCnt2 = Number((await q(`SELECT count(*) c FROM mu.schema_migrations`)).rows[0].c);
  ok('S4h 迁移重放幂等（仍 25 行）', migCnt2 === 25, migCnt2);

  console.log(`\n  migration-drill v23→v25: ${pass} pass, ${fail} fail`);
  process.exit(fail > 0 ? 1 : 0);
} catch (e) {
  console.error('DRILL-ERROR', e?.stack ?? e);
  process.exit(1);
}
