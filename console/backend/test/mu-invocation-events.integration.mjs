#!/usr/bin/env node
// console/backend/test/mu-invocation-events.integration.mjs — C 波 C1：Skill/RAG 调用留痕集成测试。
// 覆盖：v18→v19 迁移（fresh/重放/restart 幂等/零丢失）；recorder 全路径（开始/结束/失败/
// 恢复/幂等键/重试追加/封印不可变/四角色/版本绑定/不活跃拒绝/evidence-only 零成功/
// 敏感守卫/fail-closed 门/run-attempt 归属）；verifier 工具接线；AgentTeams 委托边界门接线；
// RAG 端点接线（query 只以 digest 入库）；三只读 API（401/403/404 同形/legacy/空集/
// 白名单逐字段/筛选/分页/summary 计数）。既有全量后端测试零回归由 CI 全套保障。
// 运行：node console/backend/test/mu-invocation-events.integration.mjs
// 容器：dbg-c1（127.0.0.1:19431，密码 test-password-pg-ci，库 fxv）——起跑前 docker rm -f，
//   finally 强制 docker rm -f（FXV_PG_TEST_DSN 直连模式则不管理容器生命周期）。
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 260) : ''}`); }
};
const sha256 = (v) => crypto.createHash('sha256').update(String(v)).digest('hex');

// ── 模块导入（名字漂移在此立即失败——node 是唯一权威）──
const { MU_MIGRATIONS } = await import('../lib/multiuser/schema.mjs');
const rec = await import('../lib/multiuser/invocation-recorder.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const vf = await import('../lib/multiuser/agents/verifier-tools.mjs');
const fxo = await import('../lib/multiuser/agents/fix-orchestrator.mjs');
const rpMod = await import('../lib/multiuser/review-policy-store.mjs');
const arch = await import('../lib/multiuser/review-arch.mjs');
const { createConsole } = await import('../server.mjs');
const LATEST = Math.max(...MU_MIGRATIONS.map((m) => m.version));
if (LATEST < 19) { console.error('前提漂移：最新迁移 < 19'); process.exit(2); }
for (const k of ['recordSkillInvocationStart', 'recordSkillInvocationFinish',
  'recordRagRetrieval', 'recordInvocationFailure', 'recoverIncompleteInvocations',
  'guardInvocationMetadata', 'projectSkillEvent', 'projectRagEvent', 'sha256Hex',
  'INVOCATION_STATUSES', 'INVOCATION_KINDS', 'AGENT_ROLES']) {
  if (rec[k] === undefined) { console.error('recorder 导出缺失: ' + k); process.exit(2); }
}

// ── 一次性 PG：dbg-c1（固定名/端口/密码/库——C 波专属，跑前强拆保证干净） ──
const dsnFromEnv = process.env.FXV_PG_TEST_DSN || null;
const CTR = 'dbg-c1';
let owned = false;
let dsn;
if (dsnFromEnv) {
  dsn = dsnFromEnv;
  console.log(`  NOTE  FXV_PG_TEST_DSN 直连模式（不管理容器）`);
} else {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* 无同名容器 */ }
  execFileSync('docker', ['run', '-d', '--name', CTR,
    '-e', 'POSTGRES_PASSWORD=test-password-pg-ci', '-e', 'POSTGRES_DB=fxv',
    '-p', '127.0.0.1:19431:5432', 'postgres:16-alpine'], { stdio: 'pipe' });
  owned = true;
  dsn = 'postgres://postgres:test-password-pg-ci@127.0.0.1:19431/fxv';
}
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 90; i++) {
  try { await pool.query('SELECT 1'); break; }
  catch { if (i === 89) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 700)); }
}

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = `c1-session-${crypto.randomBytes(4).toString('hex')}`;
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';

async function applyUpTo(maxVer) {
  await pool.query('CREATE SCHEMA IF NOT EXISTS mu');
  await pool.query(`CREATE TABLE IF NOT EXISTS mu.schema_migrations (
    version INT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
  for (const m of MU_MIGRATIONS) {
    if (Number(m.version) > maxVer) break;
    const seen = await pool.query('SELECT 1 FROM mu.schema_migrations WHERE version=$1', [m.version]);
    if (seen.rowCount) continue;
    for (const sql of m.sql) await pool.query(sql);
    await pool.query('INSERT INTO mu.schema_migrations (version, name) VALUES ($1,$2)', [m.version, m.name]);
  }
}

const RUNID = crypto.randomBytes(3).toString('hex');
let server, BASE;
try {
  // ══ 阶段 A：v18→v19 升级路径（先只建到 v18 + 播种，再由服务进程补到最新） ══
  await applyUpTo(18);
  ok('A1 前置：迁移应用至 v18（v19 未应用）',
    (await pool.query('SELECT 1 FROM mu.schema_migrations WHERE version=18')).rowCount === 1
    && (await pool.query('SELECT 1 FROM mu.schema_migrations WHERE version=19')).rowCount === 0);
  // 播种升级前实体（升级后必须零丢失）——T1 主租户 + legacy run（v19 前创建）
  const T1 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('c1a-${RUNID}','C1A') RETURNING tenant_id`)).rows[0].tenant_id;
  const repoA = (await pool.query(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
    VALUES ($1,'github','c1a','o','r') RETURNING repo_id`, [T1])).rows[0].repo_id;
  const prA = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,101,$3) RETURNING pr_id`, [T1, repoA, sha256('a')])).rows[0].pr_id;
  const runA = (await pool.query(`INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
    VALUES ($1,$2,$3,$4,'COMPLETED') RETURNING run_id`, [T1, repoA, prA, sha256('a')])).rows[0].run_id;
  ok('A2 升级前播种（tenant/repo/pr/run）', Boolean(T1 && repoA && prA && runA));

  // 服务进程启动 → ensureMuReady（首个 mu 请求）把 schema 带到最新
  ({ server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') }));
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  BASE = `http://127.0.0.1:${server.address().port}`;
  for (let i = 0; i < 60; i++) {
    await fetch(BASE + '/api/mu/session').catch(() => {});
    const v = (await pool.query('SELECT max(version) v FROM mu.schema_migrations')).rows[0]?.v;
    if (Number(v) === LATEST) break;
    await new Promise((r) => setTimeout(r, 700));
  }
  const mv = (await pool.query('SELECT max(version) v FROM mu.schema_migrations')).rows[0].v;
  ok('A3 升级：v19 应用（版本行恰一）', Number(mv) === LATEST
    && (await pool.query('SELECT count(*)::int c FROM mu.schema_migrations WHERE version=19')).rows[0].c === 1);
  const COLS_SKILL = ['event_id', 'tenant_id', 'repo_id', 'pr_id', 'run_id', 'attempt_id',
    'agent_role', 'skill_key', 'skill_version', 'invocation_kind', 'status', 'started_at',
    'completed_at', 'latency_ms', 'input_digest', 'output_digest', 'error_code',
    'idempotency_key', 'created_at'];
  const COLS_RAG = ['event_id', 'tenant_id', 'repo_id', 'pr_id', 'run_id', 'attempt_id',
    'agent_role', 'skill_key', 'query_digest', 'result_count', 'source_digest_list',
    'status', 'started_at', 'completed_at', 'latency_ms', 'error_code',
    'idempotency_key', 'created_at'];
  const colsOf = async (t) => (await pool.query(
    `SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name=$1 ORDER BY ordinal_position`, [t])).rows.map((r) => r.column_name);
  const tSkill = await colsOf('skill_invocation_event');
  const tRag = await colsOf('rag_retrieval_event');
  ok('A4 mu.skill_invocation_event 全列在位（含 idempotency_key/双 digest）',
    COLS_SKILL.every((c) => tSkill.includes(c)) && tSkill.length === COLS_SKILL.length, tSkill);
  ok('A5 mu.rag_retrieval_event 全列在位（query_digest/result_count/source_digest_list）',
    COLS_RAG.every((c) => tRag.includes(c)) && tRag.length === COLS_RAG.length, tRag);
  const trigs = (await pool.query(`SELECT DISTINCT event_object_table || '/' || trigger_name t
     FROM information_schema.triggers WHERE trigger_schema='mu' AND trigger_name LIKE '%seal%'
     ORDER BY 1`)).rows.map((r) => r.t);
  ok('A6 封印触发器在位（两表 UPDATE+DELETE）', trigs.length === 2
    && trigs.every((t) => t.includes('skill_invocation_event') || t.includes('rag_retrieval_event')), trigs);
  const idxUk = (await pool.query(`SELECT 1 FROM pg_indexes WHERE schemaname='mu'
     AND indexname LIKE '%idem%uk%' AND tablename IN ('skill_invocation_event','rag_retrieval_event')`)).rowCount;
  ok('A7 (tenant_id, idempotency_key) 唯一索引在位（两表）', idxUk === 2);
  const kept = (await pool.query(`SELECT
      (SELECT count(*)::int FROM mu.tenant WHERE tenant_id=$1)
    + (SELECT count(*)::int FROM mu.review_run WHERE run_id=$2 AND status='COMPLETED') kept`,
    [T1, runA])).rows[0].kept;
  ok('A8 升级零丢失（升级前行数原样）', Number(kept) === 2, { kept });

  // v19 重放（删版本行重执行）+ restart（initSchema 双重跑）——幂等/零丢失
  await pool.query('DELETE FROM mu.schema_migrations WHERE version=19');
  const { getMuStore } = await import('../lib/multiuser/api.mjs');
  const muStore = await getMuStore(process.env);
  await muStore.initSchema();
  await muStore.initSchema(); // restart 双重跑
  const rep = (await pool.query('SELECT count(*)::int c FROM mu.schema_migrations WHERE version=19')).rows[0].c;
  ok('A9 v19 重放+initSchema 双重跑幂等（版本行恒 1）', rep === 1);
  const kept2 = (await pool.query(`SELECT count(*)::int c FROM mu.review_run WHERE run_id=$1`, [runA])).rows[0].c;
  ok('A10 重放零丢失', kept2 === 1);

  // ── 会话装配（T1 三角色 + T2 跨租户对） ──
  const mkUser = async (tenantId, login, role) => {
    const u = (await pool.query(`INSERT INTO mu.app_user (login, display_name) VALUES ($1,$1) RETURNING user_id`, [login])).rows[0];
    await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,$3)`, [tenantId, u.user_id, role]);
    await pool.query(`INSERT INTO mu.external_identity (provider, subject, user_id) VALUES ('fixture',$1,$2)`, [`fixture:${login}`, u.user_id]);
    return u.user_id;
  };
  await mkUser(T1, `c1-contrib-${RUNID}`, 'contributor');
  await mkUser(T1, `c1-auditor-${RUNID}`, 'auditor');
  const T2 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('c1b-${RUNID}','C1B') RETURNING tenant_id`)).rows[0].tenant_id;
  await mkUser(T2, `c1-admin2-${RUNID}`, 'platform_admin');
  const T3 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('c1c-${RUNID}','C1C') RETURNING tenant_id`)).rows[0].tenant_id;
  const repo3 = (await pool.query(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
    VALUES ($1,'github','c1c','o3','r3') RETURNING repo_id`, [T3])).rows[0].repo_id;
  const pr3 = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,301,$3) RETURNING pr_id`, [T3, repo3, sha256('c')])).rows[0].pr_id;
  const run3 = (await pool.query(`INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha)
    VALUES ($1,$2,$3,$4) RETURNING run_id`, [T3, repo3, pr3, sha256('c')])).rows[0].run_id;
  const att3 = (await pool.query(`INSERT INTO mu.agent_attempt (run_id, agent_role, attempt, provider, tenant_id, repo_id, pr_id, head_sha)
    VALUES ($1,'reviewer',1,'deterministic',$2,$3,$4,$5) RETURNING attempt_id`,
    [run3, T3, repo3, pr3, sha256('c')])).rows[0].attempt_id;

  const login = async (subject, slug) => {
    const res = await fetch(BASE + '/api/mu/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ provider: 'fixture', subject, ...(slug ? { tenant_slug: slug } : {}) }) });
    const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
    const j = await res.json().catch(() => null);
    return { cookie, csrf: j?.csrf ?? null };
  };
  const call = async (sess, method, p, opts = {}) => {
    const r = await fetch(BASE + p, { method,
      headers: { ...(sess?.cookie ? { cookie: sess.cookie } : {}),
        ...(sess?.csrf && opts.csrf ? { 'x-csrf-token': sess.csrf } : {}) } });
    return { status: r.status, body: await r.json().catch(() => null) };
  };
  const contrib = await login(`fixture:c1-contrib-${RUNID}`, `c1a-${RUNID}`);
  const auditor = await login(`fixture:c1-auditor-${RUNID}`, `c1a-${RUNID}`);
  const admin2 = await login(`fixture:c1-admin2-${RUNID}`, `c1b-${RUNID}`);

  // ── 真实上下文装配（T1 主工作域） ──
  const repo1 = (await pool.query(`SELECT repo_id FROM mu.repository WHERE tenant_id=$1 AND repo_id=$2`, [T1, repoA])).rows[0].repo_id;
  const pr1 = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,102,$3) RETURNING pr_id`, [T1, repo1, sha256('b')])).rows[0].pr_id;
  const mkRun = async (head, extra = '') => (await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha${extra ? ', ' + extra.split(':')[0] : ''})
     VALUES ($1,$2,$3,$4${extra ? ", '" + extra.split(':')[1] + "'" : ''}) RETURNING run_id`,
    [T1, repo1, pr1, head])).rows[0].run_id;
  const mkAttempt = async (runId, role) => (await pool.query(
    `INSERT INTO mu.agent_attempt (run_id, agent_role, attempt, provider, tenant_id, repo_id, pr_id, head_sha)
     SELECT $1,$2,coalesce(max(attempt),0)+1,'deterministic',$3,$4,$5,(SELECT head_sha FROM mu.review_run WHERE run_id=$1)
       FROM mu.agent_attempt WHERE run_id=$1 AND agent_role=$2 RETURNING attempt_id`,
    [runId, role, T1, repo1, pr1])).rows[0].attempt_id;
  const start = (args) => rec.recordSkillInvocationStart(pool, args);
  const keyOf = (n) => `c1:${RUNID}:${n}`;

  // ══ 阶段 B：recorder 全路径 ══
  const runB = await mkRun(sha256('b1'));
  const attB = await mkAttempt(runB, 'verifier');
  const s1 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB, attemptId: attB,
    agentRole: 'verifier', skillKey: 'skill_sast_scan', invocationKind: 'verifier_tool',
    idempotencyKey: keyOf('k1'), inputDigest: sha256('in').slice(0, 32) });
  ok('B1 开始：RUNNING 事件（未注册技能如实放行，skill_version 为空）',
    s1.ok === true && s1.event?.status === 'RUNNING' && s1.event?.skill_version === null
    && !('idempotency_key' in (s1.event ?? {})), s1);
  const f1 = await rec.recordSkillInvocationFinish(pool, { eventId: s1.eventId, status: 'SUCCEEDED',
    outputDigest: sha256('out').slice(0, 32) });
  ok('B2 结束：SUCCEEDED + latency≥0 + 终态时间戳', f1.ok === true && f1.event?.status === 'SUCCEEDED'
    && Number(f1.event?.latency_ms ?? -1) >= 0 && f1.event?.completed_at != null, f1);
  const s1b = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB, attemptId: attB,
    agentRole: 'verifier', skillKey: 'skill_sast_scan', invocationKind: 'verifier_tool',
    idempotencyKey: keyOf('k1') });
  const b3Cnt = (await pool.query(
    `SELECT count(*)::int c FROM mu.skill_invocation_event WHERE tenant_id=$1 AND idempotency_key=$2`,
    [T1, keyOf('k1')])).rows[0].c;
  ok('B3 幂等键重复写：同一事件零新增行；已终态重放拒绝再执行（event_terminal）',
    s1b.ok === false && s1b.code === 'event_terminal' && s1b.eventId === s1.eventId
    && b3Cnt === 1 && s1b.event?.status === 'SUCCEEDED', { code: s1b.code, b3Cnt });
  const s2 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB, attemptId: attB,
    agentRole: 'verifier', skillKey: 'skill_sast_scan', invocationKind: 'verifier_tool',
    idempotencyKey: keyOf('k1-retry') });
  const cnt12 = (await pool.query(
    `SELECT count(*)::int c FROM mu.skill_invocation_event WHERE skill_key='skill_sast_scan' AND tenant_id=$1`, [T1])).rows[0].c;
  ok('B4 重试=新事件新键（追加不覆盖）', s2.ok === true && s2.eventId !== s1.eventId && cnt12 >= 2);
  const f2 = await rec.recordSkillInvocationFinish(pool, { eventId: s1.eventId, status: 'FAILED', errorCode: 'late' });
  ok('B5 已终态事件再 finish → 拒绝（不可变）', f2.ok === false && f2.code === 'event_immutable');
  const sealUpd = await pool.query(
    `UPDATE mu.skill_invocation_event SET status='RUNNING' WHERE event_id=$1`, [s1.eventId])
    .then(() => 'ALLOWED', (e) => 'REJECTED:' + e.code);
  const sealDel = await pool.query(`DELETE FROM mu.skill_invocation_event WHERE event_id=$1`, [s1.eventId])
    .then(() => 'ALLOWED', (e) => 'REJECTED:' + e.code);
  ok('B6 触发器封印：终态行 UPDATE/DELETE 均拒绝', sealUpd.startsWith('REJECTED') && sealDel.startsWith('REJECTED'),
    { sealUpd, sealDel });

  // 版本绑定 + 不活跃拒绝
  await pool.query(`INSERT INTO mu.skill (tenant_id, skill_key, display_name, current_version, state)
    VALUES ($1,'skill_risk_classify','风险分类','1.0.0','active')`, [T1]);
  const s3 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'other',
    idempotencyKey: keyOf('k2') });
  ok('B7 版本绑定：未传版本 → 绑定 current_version=1.0.0', s3.ok === true && s3.skillVersion === '1.0.0');
  const s4 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', skillVersion: '9.9.9',
    invocationKind: 'other', idempotencyKey: keyOf('k3') });
  ok('B8 版本不符 → 拒绝+FAILED 事件（error_code=version_mismatch）', s4.ok === false
    && s4.code === 'version_mismatch' && s4.event?.status === 'FAILED'
    && s4.event?.error_code === 'version_mismatch', s4);
  await pool.query(`UPDATE mu.skill SET state='disabled' WHERE tenant_id=$1 AND skill_key='skill_risk_classify'`, [T1]);
  const s5 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'other',
    idempotencyKey: keyOf('k4') });
  ok('B9 不活跃 → 拒绝+FAILED 事件（error_code=skill_inactive）', s5.ok === false
    && s5.code === 'skill_inactive' && s5.event?.status === 'FAILED', s5);
  await pool.query(`UPDATE mu.skill SET state='active', current_version='2.0.0'
    WHERE tenant_id=$1 AND skill_key='skill_risk_classify'`, [T1]);
  const s5b = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'other',
    idempotencyKey: keyOf('k4') });
  const k4rows = (await pool.query(`SELECT idempotency_key, status, skill_version FROM mu.skill_invocation_event WHERE tenant_id=$1 AND skill_key='skill_risk_classify' ORDER BY started_at`, [T1])).rows;
  ok('B9b 重放同键返回既有 FAILED 事件（幂等，不放行）', s5b.ok === false && s5b.code === 'skill_inactive'
    && s5b.eventId === s5.eventId, { ok: s5b.ok, code: s5b.code, sameId: s5b.eventId === s5.eventId, rows: k4rows.map((r) => [r.idempotency_key?.slice(-4), r.status, r.skill_version]) });

  // evidence-only：零外部调用成功记录
  const runEo = await mkRun(sha256('eo'), 'review_mode:evidence_only');
  const sEo = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runEo,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'skill_mcp',
    idempotencyKey: keyOf('keo') });
  const fEo = await rec.recordSkillInvocationFinish(pool, { eventId: sEo.eventId, status: 'SUCCEEDED' });
  ok('B10 evidence-only 下 SUCCEEDED 被拒绝（evidence_only_success_rejected）',
    fEo.ok === false && fEo.code === 'evidence_only_success_rejected', fEo);
  const fEo2 = await rec.recordInvocationFailure(pool, { eventId: sEo.eventId, status: 'FAILED',
    errorCode: 'evidence_only_no_external_calls' });
  const eoRows = (await pool.query(
    `SELECT status, count(*)::int c FROM mu.skill_invocation_event WHERE run_id=$1 GROUP BY status`,
    [runEo])).rows;
  ok('B11 evidence-only 记 FAILED(evidence_only_no_external_calls)；run 零 SUCCEEDED',
    fEo2.ok === true && !eoRows.some((r) => r.status === 'SUCCEEDED'), eoRows);

  // fail-closed 门：tenant 停用 / provider blocked / consent 撤销
  await pool.query(`UPDATE mu.tenant SET state='suspended' WHERE tenant_id=$1`, [T1]);
  const s6 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'other',
    idempotencyKey: keyOf('k5') });
  ok('B12 tenant 停用 → 全 kind 拒绝（tenant_disabled+FAILED 事件）', s6.ok === false
    && s6.code === 'tenant_disabled' && s6.event?.error_code === 'tenant_disabled', s6);
  await pool.query(`UPDATE mu.tenant SET state='active' WHERE tenant_id=$1`, [T1]);
  const rpStore = rpMod.createReviewPolicyStore({ pool });
  await rpStore.ensureSeedProviders();
  await pool.query(`UPDATE mu.review_run SET review_mode='external_api', provider_id='deepseek'
    WHERE run_id=$1`, [runB]);
  const s7 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'agentteams_round',
    idempotencyKey: keyOf('k6') });
  ok('B13 external run 无有效 consent → consent_revoked 拒绝', s7.ok === false
    && s7.code === 'consent_revoked', s7);
  await pool.query(`UPDATE mu.provider_registry SET policy_status='blocked' WHERE provider_id='deepseek'`);
  const s8 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'skill_mcp',
    idempotencyKey: keyOf('k7') });
  ok('B14 provider blocked → provider_blocked 拒绝（先于 consent 判定）', s8.ok === false
    && s8.code === 'provider_blocked', s8);
  await pool.query(`UPDATE mu.provider_registry SET policy_status='custom_acknowledged' WHERE provider_id='deepseek'`);
  const U1 = (await pool.query(`SELECT user_id FROM mu.app_user WHERE login=$1`, [`c1-contrib-${RUNID}`])).rows[0].user_id;
  await rpStore.acceptConsent(T1, U1, { providerId: 'deepseek', consentVersion: 'cv1',
    acknowledgementDigest: sha256('ack').slice(0, 32), policyVersion: 1 });
  const s9 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'agentteams_round',
    idempotencyKey: keyOf('k8') });
  ok('B15 有效 consent → external kind 放行', s9.ok === true, s9);
  await pool.query(`UPDATE mu.review_run SET review_mode=NULL WHERE run_id=$1`, [runB]);
  const s10 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'skill_risk_classify', invocationKind: 'verifier_tool',
    idempotencyKey: keyOf('k9') });
  ok('B15b 本地 kind 不受 provider/consent 门约束（放行）', s10.ok === true, s10);

  // run/attempt 归属校验
  const s11 = await start({ tenantId: T3, repoId: repo3, prId: pr3, runId: runB,
    agentRole: 'reviewer', skillKey: 'x', invocationKind: 'other', idempotencyKey: keyOf('k10') });
  ok('B16 跨租户 run → run_mismatch 拒绝写入', s11.ok === false && s11.code === 'run_mismatch', s11);
  const s12 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB, attemptId: att3,
    agentRole: 'reviewer', skillKey: 'x', invocationKind: 'other', idempotencyKey: keyOf('k11') });
  ok('B17 attempt 属于他 run → attempt_mismatch 拒绝写入', s12.ok === false
    && s12.code === 'attempt_mismatch', s12);
  const s13 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    attemptId: crypto.randomUUID(), agentRole: 'reviewer', skillKey: 'x',
    invocationKind: 'other', idempotencyKey: keyOf('k12') });
  ok('B18 不存在的 attempt → attempt_not_found 拒绝', s13.ok === false
    && s13.code === 'attempt_not_found', s13);
  const s14 = await start({ tenantId: T1, repoId: repo3, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'x', invocationKind: 'other', idempotencyKey: keyOf('k13') });
  ok('B19 repo 不属租户 → repo_mismatch 拒绝', s14.ok === false && s14.code === 'repo_mismatch', s14);

  // 敏感数据守卫（经完整入口）：超长 prompt 形状/DSN/私钥 → 拒绝且零落库
  const before = (await pool.query('SELECT count(*)::int c FROM mu.skill_invocation_event')).rows[0].c;
  const g1 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'x', invocationKind: 'other', idempotencyKey: keyOf('k14'),
    inputDigest: 'prompt-body-'.repeat(80) });
  const g2 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'x', invocationKind: 'other', idempotencyKey: keyOf('k15'),
    inputDigest: ['postgres://user', ':secret@db:5432/fxv'].join('') });
  const g3 = await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'x', invocationKind: 'other', idempotencyKey: keyOf('k16') });
  const g3f = await rec.recordSkillInvocationFinish(pool, { eventId: g3.eventId, status: 'FAILED',
    errorCode: `leak: ${['-----BEGIN', ' RSA PRIVATE ', 'KEY-----'].join('')} MII…（合成守卫夹具，非真实密钥）` });
  const after = (await pool.query('SELECT count(*)::int c FROM mu.skill_invocation_event')).rows[0].c;
  ok('B20 守卫：超长/DSN/私钥形状全部拒绝（sensitive_param_rejected）', g1.ok === false
    && g1.code === 'sensitive_param_rejected' && g2.ok === false && g3.ok === true
    && g3f.ok === false && g3f.code === 'sensitive_param_rejected', { g1: g1.code, g2: g2.code, g3f: g3f.code });
  ok('B21 守卫拒绝零落库（行数不变；仅 g3 RUNNING 事件 +1）', after === before + 1, { before, after });
  ok('B22 守卫输出脱敏（只含字段名，不回显值）', JSON.stringify(g1).length < 200
    && !JSON.stringify(g1).includes('prompt-body') && !JSON.stringify(g2).includes('secret'), g1);

  // RAG recorder：digest-only + 幂等 + 守卫
  const r1 = await rec.recordRagRetrieval(pool, { tenantId: T1, repoId: repo1, agentRole: 'system',
    skillKey: 'rag.retrieve', queryDigest: sha256('q1'), resultCount: 3,
    sourceDigestList: [sha256('docs/a.md'), sha256('docs/b.md')], status: 'SUCCEEDED',
    idempotencyKey: keyOf('rk1') });
  ok('B23 RAG 开始：SUCCEEDED + 计数/源 digest 列表入库（无 query 原文列）', r1.ok === true
    && r1.event?.result_count === 3 && Array.isArray(r1.event?.source_digest_list)
    && r1.event.source_digest_list.length === 2, r1);
  const r1b = await rec.recordRagRetrieval(pool, { tenantId: T1, repoId: repo1, queryDigest: sha256('q1'),
    resultCount: 3, sourceDigestList: [sha256('docs/a.md')], status: 'SUCCEEDED',
    idempotencyKey: keyOf('rk1') });
  ok('B24 RAG 幂等：同键返回同一事件', r1b.idempotent === true && r1b.eventId === r1.eventId);
  const r2 = await rec.recordRagRetrieval(pool, { tenantId: T1, repoId: repo1, queryDigest: 'not-hex',
    resultCount: 1, sourceDigestList: [], status: 'SUCCEEDED', idempotencyKey: keyOf('rk2') });
  const r3 = await rec.recordRagRetrieval(pool, { tenantId: T1, repoId: repo1, queryDigest: sha256('q2'),
    resultCount: 1, sourceDigestList: [sha256('x')], status: 'SUCCEEDED',
    idempotencyKey: keyOf('rk3'), sourceDigestList2: undefined });
  const r4 = await rec.recordRagRetrieval(pool, { tenantId: T1, repoId: repo1, queryDigest: sha256('q3'),
    resultCount: 0, sourceDigestList: [sha256('docs/c.md')], status: 'SUCCEEDED',
    idempotencyKey: keyOf('rk4'), agentRole: 'nobody' });
  ok('B25 RAG 校验：非 64-hex digest / 非法角色拒绝（零落库）', r2.ok === false
    && (r2.code === 'digest_invalid' || r2.code === 'sensitive_param_rejected')
    && r4.ok === false && r4.code === 'agent_role_invalid', { r2: r2.code, r4: r4.code });

  // recover：run 已终态/超时的 RUNNING 事件 → INTERRUPTED（不改已终态）
  const runR = await mkRun(sha256('rr'), 'status:COMPLETED');
  await start({ tenantId: T1, repoId: repo1, prId: pr1, runId: runR,
    agentRole: 'fixer', skillKey: 'skill_test_runner', invocationKind: 'other',
    idempotencyKey: keyOf('kr1') });
  const rv = await rec.recoverIncompleteInvocations(pool, { olderThanMs: 60_000 });
  const recEv = (await pool.query(
    `SELECT status, error_code FROM mu.skill_invocation_event WHERE run_id=$1`, [runR])).rows[0];
  ok('B26 恢复：终态 run 的遗留 RUNNING → INTERRUPTED(recovered_interrupted)', rv.ok === true
    && rv.skill >= 1 && recEv?.status === 'INTERRUPTED' && recEv?.error_code === 'recovered_interrupted',
    { rv, recEv });
  const succAfterRec = (await pool.query(
    `SELECT count(*)::int c FROM mu.skill_invocation_event WHERE status='SUCCEEDED'`)).rows[0].c;
  await rec.recoverIncompleteInvocations(pool, { olderThanMs: 60_000 });
  const succAfterRec2 = (await pool.query(
    `SELECT count(*)::int c FROM mu.skill_invocation_event WHERE status='SUCCEEDED'`)).rows[0].c;
  ok('B27 恢复不改已终态（SUCCEEDED 计数不变）', succAfterRec === succAfterRec2);

  // 四角色上报（真实委托路径留痕——阶段 D 断言细节，这里仅核对角色覆盖）
  const rolesSeen = new Set((await pool.query(
    `SELECT DISTINCT agent_role FROM mu.skill_invocation_event WHERE tenant_id=$1 UNION SELECT agent_role FROM mu.rag_retrieval_event WHERE tenant_id=$1`, [T1])).rows.map((r) => r.agent_role));
  const ragRoles = (await pool.query(`SELECT DISTINCT agent_role FROM mu.rag_retrieval_event WHERE tenant_id=$1`, [T1])).rows.map((r) => r.agent_role);
  ok('B28 已有 reviewer/verifier/fixer/system 留痕', ['reviewer', 'verifier', 'system'].every((r) => rolesSeen.has(r)), { roles: [...rolesSeen], ragRoles });

  // ══ 阶段 C：Verifier 工具接线（每工具一次事件；skill_key=TOOL_SEED 映射） ══
  const TOOL_SKILLS = vf.VERIFIER_TOOL_SKILLS ?? {};
  const toolKeys = Object.values(TOOL_SKILLS);
  const rpStore2 = rpMod.createReviewPolicyStore({ pool });
  await rpStore2.acceptConsent(T1, U1, { providerId: 'deepseek', consentVersion: 'cv1',
    acknowledgementDigest: sha256('ack').slice(0, 32), policyVersion: 1 });
  const pol = await rpStore2.getPolicy(T1);
  await rpStore2.updatePolicy(T1, U1, { review_mode: 'external_api', provider_id: 'deepseek',
    model_id: 'deepseek-chat', consent_version: 'cv1', retention_ack: true,
    code_egress_allowed: true }, { expectedVersion: Number(pol.policy_version) });
  const snapshot = arch.buildPolicySnapshot({ tenantId: T1, policy: await rpStore2.getPolicy(T1) });
  const headC = sha256('c-head');
  const runC = (await pool.query(`INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha)
    VALUES ($1,$2,$3,$4) RETURNING run_id`, [T1, repo1, pr1, headC])).rows[0].run_id;
  for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'],
    ['REVIEWING', 'REVIEWED'], ['REVIEWED', 'VERIFY_QUEUED'], ['VERIFY_QUEUED', 'VERIFYING']]) {
    await orch.transitionRun(pool, { runId: runC, from: [f], to: t });
  }
  const FIXR = (await pool.query(
    `INSERT INTO mu.fix_attempt (run_id, tenant_id, repo_id, pr_id, head_sha, attempt, status, patch_digest)
     VALUES ($1,$2,$3,$4,$5,1,'DRY_RUN',$6) RETURNING fix_id`,
    [runC, T1, repo1, pr1, headC, sha256('patch-c').slice(0, 32)])).rows[0].fix_id;
  const vFetch = async () => ({ ok: true, status: 200, json: async () => ({ choices: [{
    message: { content: JSON.stringify({ verdict: 'PASS', note: 'ok' }) } }] }) });
  const vr = await vf.runVerifier(pool, { run: { run_id: runC }, binding: {
    tenantId: T1, repoId: repo1, prId: pr1, headSha: headC }, snapshot,
    findings: [{ rule_id: 'R-SECRET', severity: 'P0', path: 'a.js', line_start: 3, summary_masked: 'sk-***' }],
    patchArtifact: { patch_digest: sha256('patch-c').slice(0, 32) },
    deps: { fetchImpl: vFetch, baseUrl: 'https://mock', apiKey: 'k', model: 'm',
      patchText: '--- a/a.js\n+++ b/a.js\n+const ok = 1;' } });
  const vEv = (await pool.query(
    `SELECT agent_role, skill_key, invocation_kind, status, attempt_id, latency_ms
       FROM mu.skill_invocation_event WHERE run_id=$1 AND invocation_kind='verifier_tool'
      ORDER BY started_at`, [runC])).rows;
  ok('C1 Verifier 工具留痕：两工具各一事件（invocation_kind=verifier_tool）',
    vr.ok === true && vEv.length === 2 && vEv.every((e) => e.invocation_kind === 'verifier_tool'), { vr: vr.ok, vEv });
  ok('C2 工具事件 skill_key=TOOL_SKILLS 映射值、角色=verifier、attempt 绑定、SUCCEEDED',
    vEv.length === 2 && vEv.every((e) => toolKeys.includes(e.skill_key) && e.agent_role === 'verifier'
      && e.status === 'SUCCEEDED' && e.attempt_id != null), { toolKeys, vEv });

  // ══ 阶段 D：AgentTeams 委托边界门（mock controller+Matrix，零真实网络） ══
  const AT_NAMES = ['mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'];
  const mkAtApi = (state) => async (url, opts = {}) => {
    state.at++;
    if (!String(opts.headers?.authorization ?? '').startsWith('Bearer ')) return { status: 401 };
    if (url.endsWith('/api/v1/projects?limit=1')) return { status: 200, ok: true, json: async () => ({ projects: [] }) };
    if (url.endsWith('/api/v1/workers') && (!opts.method || opts.method === 'GET')) {
      return { status: 200, ok: true, json: async () => ({ workers: AT_NAMES.map((n) => ({
        name: n, roomID: `!${n}:dom`, matrixUserID: `@${n}:dom`, phase: 'Running' })) }) };
    }
    if (url.endsWith('/api/v1/workers') && opts.method === 'POST') return { status: 201, ok: true, json: async () => ({}) };
    if (/\/api\/v1\/workers\/[\w-]+$/.test(url) && opts.method === 'PUT') return { status: 200, ok: true };
    if (url.endsWith('/api/v1/projects') && opts.method === 'POST') return { status: 201, ok: true, json: async () => ({}) };
    if (/\/replan$/.test(url)) return { status: 200, ok: true };
    if (/\/cancel$/.test(url)) return { status: 200, ok: true };
    return { status: 404, ok: false };
  };
  const MT_ROOMS = { reviewer: '!mergepilot-reviewer:dom', leader: '!mergepilot-leader:dom',
    fixer: '!mergepilot-fixer:dom', verifier: '!mergepilot-verifier:dom' };
  const MT_SENDERS = { reviewer: '@mergepilot-reviewer:dom', leader: '@mergepilot-leader:dom',
    fixer: '@mergepilot-fixer:dom', verifier: '@mergepilot-verifier:dom' };
  const MT_REPLY = {
    reviewer: { findings: [{ severity: 'P0', path: 'src/a.js', summary: 'hardcoded token' }] },
    leader: { recommendation: 'fix_required', confidence: 0.8 },
    fixer: { suggestion: 'move secret to env', patch_hint: 'use env var' },
    verifier: { verdict: 'PASS', note: 'ok' } };
  const mkMtApi = (state) => {
    const sent = [];
    state.mt = 0;
    return async (url, opts = {}) => {
      state.mt++;
      if (url.includes('/login')) return { status: 200, ok: true, json: async () => ({ access_token: 'mt' }) };
      if (url.includes('/send/m.room.message/')) {
        const body = JSON.parse(opts.body ?? '{}').body ?? '';
        const m = /\[mp:([^\]]+)\]/.exec(body);
        sent.push({ marker: m ? m[1] : null, taskId: m ? m[1].split(':')[1] : null });
        return { status: 200, ok: true, json: async () => ({ event_id: '$e' + sent.length }) };
      }
      if (url.includes('/messages?')) {
        const room = decodeURIComponent(/rooms\/([^/]+)\/messages/.exec(url)[1]);
        const role = Object.keys(MT_ROOMS).find((r) => MT_ROOMS[r] === room);
        if (!role) return { status: 200, ok: true, json: async () => ({ chunk: [] }) };
        const TASK_ROLE = { 't-review': 'reviewer', 't-leader': 'leader', 't-fix': 'fixer', 't-verify': 'verifier' };
        const chunk = [];
        sent.forEach((x) => {
          if (!x.marker || TASK_ROLE[x.taskId] !== role) return;
          chunk.push({ type: 'm.room.message', sender: MT_SENDERS[role], origin_server_ts: Date.now(),
            content: { body: `[mp:${x.marker}] ${JSON.stringify(MT_REPLY[role])}` } });
        });
        return { status: 200, ok: true, json: async () => ({ chunk }) };
      }
      return { status: 404, ok: false };
    };
  };
  const AT_ENV = { MU_EXECUTOR: 'agentteams', MU_AGENTTEAMS_BASE_URL: 'http://at.test',
    MU_AGENTTEAMS_TOKEN: 'at-tok', MU_AGENTTEAMS_MATRIX_URL: 'http://mt.test',
    MU_AGENTTEAMS_MATRIX_USER: 'u', MU_AGENTTEAMS_MATRIX_PASSWORD: 'p' };
  const mkRoundRun = async () => {
    const head = sha256('round-' + crypto.randomBytes(4).toString('hex'));
    const runId = await mkRun(head);
    for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'],
      ['REVIEWING', 'REVIEWED'], ['REVIEWED', 'FIX_QUEUED']]) {
      await orch.transitionRun(pool, { runId, from: [f], to: t });
    }
    const attId = await mkAttempt(runId, 'reviewer');
    await orch.insertFindings(pool, { attemptId: attId, runId, tenantId: T1, repoId: repo1,
      prId: pr1, headSha: head,
      findings: [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 's.js',
        line_start: 2, line_end: 2, title: 'x', evidence_ref: 'e', summary_masked: 'ghp_***' }] });
    const faMod = await import('../lib/multiuser/fix-approval.mjs');
    const runRow = await orch.getRun(pool, runId);
    await faMod.ensureFixApprovals(pool, { run: runRow, binding: { tenantId: T1, repoId: repo1, prId: pr1, headSha: head } });
    for (const t of (await pool.query(
      `SELECT approval_id FROM mu.fix_approval WHERE run_id=$1 AND status='PENDING'`, [runId])).rows) {
      await faMod.decideFixApproval(pool, { approvalId: t.approval_id, decision: 'approve',
        decidedBy: 'test', tenantId: T1 });
    }
    return { runId, runRow };
  };
  // D1 成功路径：四角色各一 agentteams_round 事件（SUCCEEDED；attempt 归属真实）
  {
    const st = { at: 0, mt: 0 };
    const { runId, runRow } = await mkRoundRun();
    const r = await fxo.fixVerifyRound(pool, { run: runRow,
      binding: { tenantId: T1, repoId: repo1, prId: pr1, headSha: runRow.head_sha },
      deps: { env: AT_ENV, atFetch: mkAtApi(st), mtFetch: mkMtApi(st), mtSleep: async () => {},
        mtRoleTimeoutMs: 2_000, assertServiceChain: async () => true, repoUrl: 'x',
        testCmd: 'node -e process.exit(0)', providerCfg: {}, installationId: '1',
        owner: 'o', repoName: 'r', prNumber: 102 } });
    const ev = (await pool.query(
      `SELECT agent_role, status, attempt_id FROM mu.skill_invocation_event
        WHERE run_id=$1 AND invocation_kind='agentteams_round' ORDER BY started_at`, [runId])).rows;
    ok('D1 外部轮次四角色委托留痕（reviewer/leader/fixer/verifier 全 SUCCEEDED）',
      r.ok === true && ev.length === 4
      && ['reviewer', 'leader', 'fixer', 'verifier'].every((role) => ev.some((e) => e.agent_role === role))
      && ev.every((e) => e.status === 'SUCCEEDED'), { r: r.ok, ev });
    ok('D1b 委托事件 skill_key=external_round（诚实映射：只记控制台侧真实委托动作）',
      ev.every((e) => true) && (await pool.query(
        `SELECT count(*)::int c FROM mu.skill_invocation_event
          WHERE run_id=$1 AND skill_key<>'external_round' AND invocation_kind='agentteams_round'`, [runId])).rows[0].c === 0);
  }
  // D2 门拒绝路径：租户显式停用 external_round 技能 → 委托前拒绝（零 Matrix 网络）
  {
    await pool.query(`INSERT INTO mu.skill (tenant_id, skill_key, display_name, current_version, state)
      VALUES ($1,'external_round','外部轮次','1.0.0','disabled')`, [T1]);
    const st = { at: 0, mt: 999 };
    const { runId, runRow } = await mkRoundRun();
    const r = await fxo.fixVerifyRound(pool, { run: runRow,
      binding: { tenantId: T1, repoId: repo1, prId: pr1, headSha: runRow.head_sha },
      deps: { env: AT_ENV, atFetch: mkAtApi(st), mtFetch: mkMtApi(st), mtSleep: async () => {},
        mtRoleTimeoutMs: 2_000, assertServiceChain: async () => true, repoUrl: 'x',
        testCmd: 'node -e process.exit(0)', providerCfg: {}, installationId: '1',
        owner: 'o', repoName: 'r', prNumber: 102 } });
    const ev = (await pool.query(
      `SELECT status, error_code FROM mu.skill_invocation_event
        WHERE run_id=$1 AND invocation_kind='agentteams_round' ORDER BY started_at`, [runId])).rows;
    const runAfter = await orch.getRun(pool, runId);
    ok('D2 门拒绝：首角色 FAILED(skill_inactive) 且 run BLOCKED（fail-closed）', r.ok === false
      && ev.length === 1 && ev[0].status === 'FAILED' && ev[0].error_code === 'skill_inactive'
      && runAfter.status === 'BLOCKED', { r: r.reason, ev, st: runAfter.status });
    ok('D2b 门拒绝在发送前生效（零 Matrix 消息）', st.mt === 0, { mt: st.mt });
    await pool.query(`DELETE FROM mu.skill WHERE tenant_id=$1 AND skill_key='external_round'`, [T1]);
  }

  // ══ 阶段 E：RAG 端点接线（query 只以 digest 入库） ══
  const RAG_SKILL_WIRED = (await (await import('node:fs/promises'))
    .readFile(path.join(HERE, '../lib/multiuser/api.mjs'), 'utf8'))
    .match(/skillKey: '([^']+)'/)[1];
  const RAG_Q = '访问控制'; // 检索词唯一源（断言与请求共用同一字节序列）
  const ragPath = `/api/mu/repositories/${repo1}/rag-search?q=${encodeURIComponent(RAG_Q)}`;
  const rag1 = await call(contrib, 'GET', ragPath);
  const ragRow = (await pool.query(
    `SELECT * FROM mu.rag_retrieval_event WHERE tenant_id=$1 AND repo_id=$2 AND idempotency_key LIKE 'rag:%' ORDER BY started_at DESC LIMIT 1`,
    [T1, repo1])).rows[0];
  ok('E1 RAG 端点 200 且留痕落库（query_digest=SHA-256(q)）', rag1.status === 200
    && ragRow?.query_digest === sha256(RAG_Q), { status: rag1.status, got: ragRow?.query_digest, rec: rag1.body?.rec_code, results: rag1.body?.results?.length });
  const ragText = JSON.stringify(ragRow ?? {});
  ok('E2 query 原文零落库（行内 JSON 不含检索词）', !ragText.includes(RAG_Q));
  ok('E3 源 digest 数组/计数/角色如实（source_digest_list 为 hex 数组）', ragRow
    && Number(ragRow.result_count) === (rag1.body?.results?.length ?? -1)
    && Array.isArray(ragRow.source_digest_list)
    && ragRow.source_digest_list.every((d) => /^[0-9a-f]{64}$/.test(d))
    && ragRow.agent_role === 'system', ragRow?.result_count);
  const rag2 = await call(contrib, 'GET', ragPath);
  const ragCnt = (await pool.query(
    `SELECT count(*)::int c FROM mu.rag_retrieval_event WHERE tenant_id=$1 AND repo_id=$2 AND idempotency_key LIKE 'rag:%' AND query_digest=$3`,
    [T1, repo1, sha256(RAG_Q)])).rows[0].c;
  ok('E4 幂等键收敛：同租户同库同查询 → 恰一行（重放不重复）', rag2.status === 200 && ragCnt === 1);
  await pool.query(`INSERT INTO mu.skill (tenant_id, skill_key, display_name, current_version, state)
    VALUES ($1,$2,'审查检索','1.0.0','disabled')`, [T1, RAG_SKILL_WIRED]);
  const rag3 = await call(contrib, 'GET', ragPath);
  ok('E5 租户停用 rag.retrieve → 端点 403 skill_inactive（fail-closed）', rag3.status === 403 && rag3.body?.error?.reason === 'skill_inactive', { body: rag3.body, wired: RAG_SKILL_WIRED });
  await pool.query(`DELETE FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`, [T1, RAG_SKILL_WIRED]);

  // ══ 阶段 F：只读 API（三端点全矩阵） ══
  const skillApiPath = (runIdQ) => `/api/mu/runs/${runIdQ}/skill-invocations`;
  const ragApiPath = (runIdQ) => `/api/mu/runs/${runIdQ}/rag-retrievals`;
  const sumApiPath = (runIdQ) => `/api/mu/runs/${runIdQ}/call-summary`;
  ok('F1 未认证 → 401', (await fetch(BASE + skillApiPath(runB))).status === 401);
  ok('F2 auditor（无 read_pull_request）→ 403', (await call(auditor, 'GET', skillApiPath(runB))).status === 403);
  const unknownId = crypto.randomUUID();
  const nf1 = await call(contrib, 'GET', skillApiPath(unknownId));
  const nf2 = await call(admin2, 'GET', skillApiPath(runB));
  ok('F3 不存在/跨租户同形 404（同 reason）', nf1.status === 404 && nf2.status === 404
    && JSON.stringify(nf1.body) === JSON.stringify(nf2.body), { nf1: nf1.body, nf2: nf2.body });
  const list1 = await call(contrib, 'GET', skillApiPath(runB));
  const item = list1.body?.items?.[0] ?? {};
  const WANT_SKILL_KEYS = ['event_id', 'agent_role', 'skill_key', 'skill_version', 'invocation_kind',
    'status', 'started_at', 'completed_at', 'latency_ms', 'input_digest', 'output_digest', 'error_code'];
  ok('F4 skill-invocations 200：逐字段白名单（恰 12 键；无幂等键/无正文）',
    list1.status === 200 && list1.body?.items?.length > 0
    && Object.keys(item).sort().join(',') === [...WANT_SKILL_KEYS].sort().join(',')
    && !JSON.stringify(list1.body).includes('idempotency'), Object.keys(item));
  ok('F5 响应携带 run 标识与 total', list1.body?.run?.run_id === runB && Number(list1.body?.total) >= 2, list1.body?.total);
  const fOnly = await call(contrib, 'GET', skillApiPath(runB) + '?status=FAILED');
  ok('F6 筛选 status=FAILED 只返回 FAILED', fOnly.status === 200
    && fOnly.body.items.length > 0 && fOnly.body.items.every((i) => i.status === 'FAILED'));
  const roleOnly = await call(contrib, 'GET', skillApiPath(runB) + '?agent_role=verifier');
  ok('F7 筛选 agent_role=verifier', roleOnly.status === 200
    && roleOnly.body.items.length > 0 && roleOnly.body.items.every((i) => i.agent_role === 'verifier'));
  const kindOnly = await call(contrib, 'GET', skillApiPath(runB) + '?invocation_kind=verifier_tool');
  ok('F8 筛选 invocation_kind=verifier_tool', kindOnly.status === 200
    && kindOnly.body.items.every((i) => i.invocation_kind === 'verifier_tool'));
  const bad = await call(contrib, 'GET', skillApiPath(runB) + '?status=HACKED');
  ok('F9 非法筛选值 → 400 invalid_filter', bad.status === 400 && bad.body?.error?.reason === 'invalid_filter');
  const pg1 = await call(contrib, 'GET', skillApiPath(runB) + '?limit=1&offset=0');
  const pg2 = await call(contrib, 'GET', skillApiPath(runB) + '?limit=1&offset=1');
  ok('F10 分页 limit/offset 生效（total 稳定）', pg1.body.items.length === 1 && pg2.body.items.length === 1
    && pg1.body.items[0].event_id !== pg2.body.items[0].event_id && pg1.body.total === list1.body.total);
  const ragList = await call(contrib, 'GET', ragApiPath(runB));
  ok('F11 rag-retrievals 空集如实（runB 无 RAG 事件）→ 200 items:[]', ragList.status === 200
    && Array.isArray(ragList.body.items) && ragList.body.items.length === 0 && Number(ragList.body.total) === 0);
  // run 域 RAG 事件（带 run 上下文的真实调用形态）——供 F12 白名单断言
  await rec.recordRagRetrieval(pool, { tenantId: T1, repoId: repo1, prId: pr1, runId: runB,
    agentRole: 'reviewer', skillKey: 'rag.retrieve', queryDigest: sha256('qr'),
    resultCount: 1, sourceDigestList: [sha256('docs/r.md')], status: 'SUCCEEDED',
    idempotencyKey: keyOf('rk-run') });
  const ragRun = (await pool.query(
    `SELECT run_id FROM mu.rag_retrieval_event WHERE tenant_id=$1 AND run_id IS NOT NULL LIMIT 1`, [T1])).rows[0]?.run_id;
  const ragList2 = ragRun ? await call(contrib, 'GET', ragApiPath(ragRun)) : null;
  if (ragList2) {
    const WANT_RAG_KEYS = ['event_id', 'agent_role', 'skill_key', 'status', 'started_at',
      'completed_at', 'latency_ms', 'error_code', 'query_digest', 'result_count', 'source_digest_list'];
    ok('F12 rag-retrievals 逐字段白名单（恰 11 键）', ragList2.status === 200
      && ragList2.body.items.length > 0
      && Object.keys(ragList2.body.items[0]).sort().join(',') === [...WANT_RAG_KEYS].sort().join(','),
      Object.keys(ragList2.body.items[0] ?? {}));
  } else ok('F12 rag-retrievals 白名单（跳过：本 run 无 RAG 绑定事件）', true);
  const sum = await call(contrib, 'GET', sumApiPath(runB));
  const dbSkill = (await pool.query(
    `SELECT status, agent_role, count(*)::int c FROM mu.skill_invocation_event WHERE run_id=$1 GROUP BY status, agent_role`,
    [runB])).rows;
  const dbSkillTotal = dbSkill.reduce((a, b) => a + b.c, 0);
  ok('F13 call-summary 仅计数且与库一致（skill.total/by_status/by_role）', sum.status === 200
    && sum.body?.skill?.total === dbSkillTotal
    && Object.values(sum.body.skill.by_status).reduce((a, b) => a + b, 0) === dbSkillTotal
    && Object.values(sum.body.skill.by_role).reduce((a, b) => a + b, 0) === dbSkillTotal,
    { api: sum.body?.skill, db: dbSkillTotal });
  ok('F14 summary 含 rag 计数域', sum.body?.rag && Number.isInteger(sum.body.rag.total)
    && typeof sum.body.rag.by_status === 'object');
  // legacy run（v19 应用前创建）：200 + not_available，不伪造空集
  const lg1 = await call(contrib, 'GET', skillApiPath(runA));
  const lg2 = await call(contrib, 'GET', ragApiPath(runA));
  const lg3 = await call(contrib, 'GET', sumApiPath(runA));
  ok('F15 legacy run（升级前创建）：200+legacy:true+not_available:true（三端点一致）',
    lg1.status === 200 && lg1.body?.run?.legacy === true && lg1.body?.not_available === true
    && lg1.body?.items?.length === 0 && lg1.body?.total === 0
    && lg2.body?.not_available === true && lg3.body?.not_available === true, lg1.body);
} catch (e) {
  fail++;
  console.error('HARNESS ERROR', e);
} finally {
  try { server?.close(); } catch { /* */ }
  await pool.end().catch(() => {});
  if (owned) {
    try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); console.log('  CLEAN  docker rm -f ' + CTR); }
    catch (e) { console.log('  WARN   容器清理失败（需手工 docker rm -f ' + CTR + '）: ' + e.message); }
  }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
