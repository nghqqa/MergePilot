#!/usr/bin/env node
// console/backend/test/mu-orchestration.integration.mjs — Wave 3 PR-A 编排状态机集成测试。
// 运行：node console/backend/test/mu-orchestration.integration.mjs（自起一次性
// postgres:16-alpine 容器，随机回环端口，跑毕 docker rm -f——绝不触碰任何常驻栈；
// 无真实凭据；不访问真实 GitHub；不依赖 MU_FIXTURES——纯数据层真实路径）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const { RUN_STATES, RUN_TRANSITIONS, TERMINAL_STATES } = orch;

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 220) : ''}`); }
};

const CTR = `mu-orch-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 16100 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PORT}/mu`;
const deadline = Date.now() + 60_000;
const pool = new Pool({ connectionString: dsn });
for (;;) {
  try { await pool.query('SELECT 1'); break; } catch (e) {
    if (Date.now() > deadline) throw new Error('PG 未就绪');
    await new Promise((r) => setTimeout(r, 400));
  }
}

try {
  const store = await createMuStore({ pool, env: { MU_BOOTSTRAP_ADMIN_LOGIN: 'orch-admin' } });
  await store.initSchema();
  await store.bootstrap();
  // ── 基座：tenant/repository/pull_request（全部服务端合成，无真实仓库）──
  const tenant = await store.ensureTenant({ slug: 'orch-it', displayName: 'Orch IT' });
  const T = tenant.tenant_id;
  const repo = await store.ensureRepository({ tenantId: T, provider: 'github',
    providerRepoId: '9001', owner: 'it-owner', name: 'it-repo', defaultBranch: 'main' });
  const pr = await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id,
    providerPrNumber: 7, headSha: 'a'.repeat(40) });
  // 第二 tenant（复合 FK 越界测试用）
  const tenantB = await store.ensureTenant({ slug: 'orch-it-b', displayName: 'Orch IT B' });

  // T0 迁移 v7 已应用
  const mig = await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=7 AND name='mu_review_orchestration'`);
  ok('T0 migration v7 applied', mig.rows.length === 1);

  // ── T1 幂等建 run：同 head 两次 → 同一 run ──
  const mk = { tenantId: T, repoId: repo.repo_id, prId: pr.pr_id, headSha: 'a'.repeat(40) };
  const r1 = await orch.createRunIfAbsent(pool, { ...mk, triggerSource: 'webhook' });
  const r2 = await orch.createRunIfAbsent(pool, { ...mk, triggerSource: 'manual' });
  ok('T1a createRunIfAbsent first created', r1.created === true && r1.run?.run_id);
  ok('T1b same head → same run (idempotent)', r2.created === false && r2.run?.run_id === r1.run.run_id);
  ok('T1c initial status RECEIVED', r1.run.status === 'RECEIVED');

  // ── T2 新 head → 新 run；旧 run 不被覆盖 ──
  const r3 = await orch.createRunIfAbsent(pool, { ...mk, headSha: 'b'.repeat(40) });
  ok('T2a new head_sha → new run', r3.created === true && r3.run.run_id !== r1.run.run_id);
  await orch.transitionRun(pool, { runId: r1.run.run_id, from: ['RECEIVED'], to: 'REVIEW_QUEUED' });
  const oldRun = await orch.getRun(pool, r1.run.run_id);
  const newRun = await orch.getRun(pool, r3.run.run_id);
  ok('T2b old head status untouched by new head', oldRun.status === 'REVIEW_QUEUED' && newRun.status === 'RECEIVED');

  // ── T3 CAS：并发/重复迁移只赢一次 ──
  const w1 = await orch.transitionRun(pool, { runId: r1.run.run_id, from: ['REVIEW_QUEUED'], to: 'REVIEWING' });
  const w2 = await orch.transitionRun(pool, { runId: r1.run.run_id, from: ['REVIEW_QUEUED'], to: 'REVIEWING' });
  ok('T3a first CAS wins', w1.ok === true);
  ok('T3b second CAS loses with cas_conflict', w2.ok === false && w2.reason === 'cas_conflict' && w2.current === 'REVIEWING');

  // ── T4 非法迁移 ──
  const bad = await orch.transitionRun(pool, { runId: r3.run.run_id, from: ['RECEIVED'], to: 'COMPLETED' });
  ok('T4 illegal transition rejected', bad.ok === false && bad.reason === 'invalid_transition');
  const nf = await orch.transitionRun(pool, { runId: crypto.randomUUID(), from: ['RECEIVED'], to: 'REVIEW_QUEUED' });
  ok('T4b unknown run → not_found', nf.ok === false && nf.reason === 'not_found');

  // ── T5 终态封死 ──
  await orch.transitionRun(pool, { runId: r1.run.run_id, from: ['REVIEWING'], to: 'REVIEWED' });
  await orch.transitionRun(pool, { runId: r1.run.run_id, from: ['REVIEWED'], to: 'COMPLETED' });
  const post = await orch.transitionRun(pool, { runId: r1.run.run_id, from: ['COMPLETED'], to: 'REVIEWING' });
  ok('T5 terminal state frozen', post.ok === false && post.reason === 'invalid_transition');

  // ── T6 attempt 原子编号 + 上限 + 死信脱敏 ──
  const attCtx = { runId: r3.run.run_id, agentRole: 'reviewer', provider: 'deterministic',
    tenantId: T, repoId: repo.repo_id, prId: pr.pr_id, headSha: 'b'.repeat(40) };
  const a1 = await orch.claimNextAttempt(pool, { ...attCtx, maxAttempts: 2 });
  const a2 = await orch.claimNextAttempt(pool, { ...attCtx, maxAttempts: 2 });
  const a3 = await orch.claimNextAttempt(pool, { ...attCtx, maxAttempts: 2 });
  ok('T6a attempts numbered atomically', a1.attempt === 1 && a2.attempt === 2);
  ok('T6b over max → max_attempts (bounded retry)', a3.ok === false && a3.reason === 'max_attempts');
  const dlqId = await orch.moveToDeadLetter(pool, { runId: r3.run.run_id, tenantId: T,
    repoId: repo.repo_id, prId: pr.pr_id, headSha: 'b'.repeat(40), agentRole: 'reviewer',
    kind: 'review_exhausted', reason: 'max_attempts=2', retryCount: 2,
    payloadRef: 'evidence://attempts/xyz' });
  const dlq = await orch.listDeadLetter(pool, {});
  const row = dlq.find((d) => d.dlq_id === dlqId);
  ok('T6c dead-letter row present with metadata', Boolean(row) && row.kind === 'review_exhausted');
  ok('T6d dead-letter leaks no payload body',
    !('payload' in row) && !('diff' in row) && !('code' in row) && typeof row.payload_ref === 'string');
  ok('T6e resolveDeadLetter closes it', await orch.resolveDeadLetter(pool, dlqId)
    && (await orch.listDeadLetter(pool, {})).every((d) => d.dlq_id !== dlqId));

  // ── T7 findings 幂等 ──
  await orch.claimNextAttempt(pool, { ...attCtx, agentRole: 'leader', provider: 'deterministic', maxAttempts: 3 });
  const la = await pool.query(
    `SELECT attempt_id FROM mu.agent_attempt WHERE run_id=$1 AND agent_role='leader' LIMIT 1`, [r3.run.run_id]);
  const findings = [{ rule_id: 'SEC001', severity: 'P1', confidence: 0.9, path: 'src/a.ts',
    line_start: 10, line_end: 12, title: 'token 硬编码', evidence_ref: 'ev://1', remediation: '改用 env' }];
  const n1 = await orch.insertFindings(pool, { attemptId: la.rows[0].attempt_id, runId: r3.run.run_id,
    tenantId: T, repoId: repo.repo_id, prId: pr.pr_id, headSha: 'b'.repeat(40), findings });
  const n2 = await orch.insertFindings(pool, { attemptId: la.rows[0].attempt_id, runId: r3.run.run_id,
    tenantId: T, repoId: repo.repo_id, prId: pr.pr_id, headSha: 'b'.repeat(40), findings });
  ok('T7 findings idempotent (dup insert adds zero rows)', n1 === 1 && n2 === 0);

  // ── T8 复合 FK：跨 tenant 组合被数据库拒绝 ──
  let fkRejected = false, fkCode = '';
  try {
    await pool.query(
      `INSERT INTO mu.agent_attempt (run_id, agent_role, attempt, provider, tenant_id, repo_id, pr_id, head_sha)
       VALUES ($1,'reviewer',9,'mock',$2,$3,$4,$5)`,
      [r3.run.run_id, tenantB.tenant_id, repo.repo_id, pr.pr_id, 'b'.repeat(40)]);
  } catch (e) { fkRejected = true; fkCode = e.code; }
  ok('T8 cross-tenant attempt rejected by composite FK', fkRejected && fkCode === '23503', { fkCode });

  // ── T9 fix/verify/decision 表 + 复合 FK ──
  const fix = await pool.query(
    `INSERT INTO mu.fix_attempt (run_id, tenant_id, repo_id, pr_id, head_sha, attempt, status, patch_digest)
     VALUES ($1,$2,$3,$4,$5,1,'DRY_RUN',$6) RETURNING fix_id`,
    [r3.run.run_id, T, repo.repo_id, pr.pr_id, 'b'.repeat(40), 'f'.repeat(64)]);
  await pool.query(
    `INSERT INTO mu.verification_attempt (run_id, fix_id, tenant_id, repo_id, pr_id, head_sha, attempt, verdict)
     VALUES ($1,$2,$3,$4,$5,$6,1,'PASS')`,
    [r3.run.run_id, fix.rows[0].fix_id, T, repo.repo_id, pr.pr_id, 'b'.repeat(40)]);
  const dId = await orch.recordDecision(pool, { runId: r3.run.run_id, tenantId: T,
    repoId: repo.repo_id, prId: pr.pr_id, headSha: 'b'.repeat(40),
    stage: 'final_verdict', decision: 'clean' });
  ok('T9 fix/verify/decision rows land with composite FK', Boolean(fix.rows[0].fix_id) && Boolean(dId));
  let verifyFkRejected = false;
  try { // fix 属于另一 run 时 verification 复合 FK 拒绝
    await pool.query(
      `INSERT INTO mu.verification_attempt (run_id, fix_id, tenant_id, repo_id, pr_id, head_sha, attempt, verdict)
       VALUES ($1,$2,$3,$4,$5,$6,1,'PASS')`,
      [r1.run.run_id, fix.rows[0].fix_id, T, repo.repo_id, pr.pr_id, 'b'.repeat(40)]);
  } catch { verifyFkRejected = true; }
  ok('T9b cross-run verification rejected', verifyFkRejected);

  // ── T10 状态机完备性 ──
  const allKeys = RUN_STATES.every((s) => Array.isArray(RUN_TRANSITIONS[s]));
  const targetsValid = Object.values(RUN_TRANSITIONS).flat().every((s) => RUN_STATES.includes(s));
  const terminalEmpty = [...TERMINAL_STATES].every((s) => RUN_TRANSITIONS[s].length === 0);
  ok('T10 transition table complete & terminal frozen', allKeys && targetsValid && terminalEmpty);

  // ── T11 latest-run 查询按 head 隔离 ──
  const latest = await orch.getLatestRunForPr(pool, { tenantId: T, repoId: repo.repo_id, prId: pr.pr_id });
  ok('T11 latest run = newest head', latest.head_sha === 'b'.repeat(40));
} catch (e) {
  fail++;
  console.error('HARNESS ERROR', e);
} finally {
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
