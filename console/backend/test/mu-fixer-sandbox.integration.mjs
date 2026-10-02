// console/backend/test/mu-fixer-sandbox.integration.mjs — PR D 门槛（mock Provider，零真实网络）。
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

const CTR = `mu-fxr-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17600 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;

const fx = await import('../lib/multiuser/agents/fixer-sandbox.mjs');
const ext = await import('../lib/multiuser/agents/external-reviewer.mjs');
const arch = await import('../lib/multiuser/review-arch.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const rpStoreMod = await import('../lib/multiuser/review-policy-store.mjs');
const store = await createMuStore({ pool });
await store.initSchema();
await store.bootstrap();
const rpStore = rpStoreMod.createReviewPolicyStore({ pool });
// v16 审批门适配：真实路径放行（ensure+逐票 approve→run 至 FIX_QUEUED）
const faMod = await import('../lib/multiuser/fix-approval.mjs');
async function approveHighRisk(run, binding) {
  await faMod.ensureFixApprovals(pool, { run, binding });
  const ts = (await pool.query(
    `SELECT approval_id FROM mu.fix_approval WHERE run_id=$1 AND status='PENDING'`, [run.run_id])).rows;
  for (const t of ts) {
    await faMod.decideFixApproval(pool, { approvalId: t.approval_id, decision: 'approve',
      decidedBy: 'test:maintainer', tenantId: binding.tenantId });
  }
}


const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
const U = (await pool.query(`SELECT user_id FROM mu.app_user LIMIT 1`)).rows[0].user_id;
const CONSENT_D = crypto.createHash('sha256').update('c-text').digest('hex').slice(0, 32);

const repo = (await pool.query(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
  VALUES ($1,'github','fxr','arch','fxr') ON CONFLICT DO NOTHING RETURNING repo_id`, [T1])).rows[0]
  ?? (await pool.query(`SELECT repo_id FROM mu.repository WHERE tenant_id=$1 LIMIT 1`, [T1])).rows[0];

async function mkRun(toState, withFindings) {
  const head = crypto.randomBytes(20).toString('hex');
  const pr = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,${Math.floor(Math.random() * 9000) + 100},$3) RETURNING pr_id`, [T1, repo.repo_id, head])).rows[0];
  const { run } = await orch.createRunIfAbsent(pool, { tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head });
  // RECEIVED→REVIEW_QUEUED→REVIEWING→REVIEWED（ Leader 决策 fix_required 时再 REVIEWED→FIX_QUEUED ）
  for (const [f, t] of [['RECEIVED','REVIEW_QUEUED'],['REVIEW_QUEUED','REVIEWING'],['REVIEWING','REVIEWED']]) {
    await orch.transitionRun(pool, { runId: run.run_id, from: [f], to: t });
  }
  if (withFindings) {
    const att = await orch.claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
      provider: 'deterministic', maxAttempts: 3, tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head });
    await orch.insertFindings(pool, { attemptId: att.attemptId, runId: run.run_id,
      tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head,
      findings: [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 'a.js',
        line_start: 3, title: 'hardcoded secret', summary_masked: 'sk-***' }] });
    await orch.finishAttempt(pool, { attemptId: att.attemptId, status: 'DONE' });
    // Leader 决策 fix_required → REVIEWED→FIX_QUEUED（走真实消费路径——非手工迁态）
    const lead = await ext.leaderConsumeFindings(pool, { run, binding: { tenantId: T1,
      repoId: repo.repo_id, prId: pr.pr_id, headSha: head }, protection: { configured: false } });
    if (lead.decision !== 'fix_required') throw new Error('setup: expected fix_required');
    await approveHighRisk(run, { tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head });
  }
  if (toState === 'REVIEWED') return { run, binding: { tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head } };
  return { run, binding: { tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head } };
}

async function externalSetup() {
  await rpStore.ensureSeedProviders();
  await rpStore.acceptConsent(T1, U, { providerId: 'deepseek', consentVersion: 'cv1',
    acknowledgementDigest: CONSENT_D, policyVersion: 1 });
  const pol = await rpStore.getPolicy(T1);
  await rpStore.updatePolicy(T1, U, { review_mode: 'external_api', provider_id: 'deepseek',
    model_id: 'deepseek-chat', consent_version: 'cv1', retention_ack: true,
    code_egress_allowed: true }, { expectedVersion: Number(pol.policy_version) });
  return arch.buildPolicySnapshot({ tenantId: T1, policy: await rpStore.getPolicy(T1) });
}

const PATCH_TEXT = '--- a/a.js\n+++ b/a.js\n@@ -3,1 +3,1 @@\n-const k = "sk-realdanger";\n+const k = process.env.KEY;';
const findingsFor = () => [{ rule_id: 'R-SECRET', severity: 'P0', path: 'a.js',
  line_start: 3, summary_masked: 'sk-***', confidence: 0.9 }];

const mkFetch = (calls) => async (url) => { calls.push(String(url));
  return { ok: true, status: 200, json: async () => ({
    choices: [{ message: { content: PATCH_TEXT } }] }) }; };

try {
  const snapshot = await externalSetup();

  // D1 状态守卫：REVIEWED（无 Leader fix_required）→ 拒绝（只有 Leader 确认后可跑）
  const s1 = await mkRun('REVIEWED', false);
  const calls1 = [];
  const d1 = await fx.runFixerSandbox(pool, { run: s1.run, binding: s1.binding,
    snapshot, findings: findingsFor(), context: { input_digest: 'd'.repeat(32) },
    deps: { fetchImpl: mkFetch(calls1), baseUrl: 'https://mock', apiKey: 'k', model: 'm' } });
  ok('D1 非 FIX_QUEUED/FIXING（无 Leader 确认）→ 拒绝+零调用', d1.ok === false && d1.stage === 'not_fix_required' && calls1.length === 0);

  // D2 egress deny（evidence_only snapshot）→ 零网络
  const s2 = await mkRun('FIX_QUEUED', true);
  const snapNo = arch.buildPolicySnapshot({ tenantId: T1,
    policy: { review_mode: 'evidence_only', policy_version: 1 } });
  const calls2 = [];
  const d2 = await fx.runFixerSandbox(pool, { run: s2.run, binding: s2.binding,
    snapshot: snapNo, findings: findingsFor(), context: { input_digest: 'd'.repeat(32) },
    deps: { fetchImpl: mkFetch(calls2), baseUrl: 'https://mock', apiKey: 'k', model: 'm' } });
  ok('D2 egress deny（evidence_only）→ 零调用可重试', d2.ok === false && d2.stage === 'egress_denied' && calls2.length === 0);

  // D3 成功链：FIX_QUEUED → patch artifact 生成 + fix_attempt DRY_RUN 落库
  const s3 = await mkRun('FIX_QUEUED', true);
  const calls3 = [];
  const d3 = await fx.runFixerSandbox(pool, { run: s3.run, binding: s3.binding,
    snapshot, findings: findingsFor(), context: { input_digest: 'e'.repeat(32),
      context_digest: 'c'.repeat(32), binding: { diff_digest: 'f'.repeat(32) },
      manifest: { redactions_applied: 1 } },
    deps: { fetchImpl: mkFetch(calls3), baseUrl: 'https://mock', apiKey: 'k', model: 'deepseek-chat' } });
  ok('D3 Fixer 成功→artifact 生成（DRY_RUN）', d3.ok === true && d3.dry_run === true);
  const fa3 = (await pool.query(
    `SELECT status, patch_digest, artifact_ref FROM mu.fix_attempt WHERE run_id=$1`, [s3.run.run_id])).rows[0];
  ok('D3b fix_attempt status=DRY_RUN（生成≠应用）', fa3?.status === 'DRY_RUN');
  ok('D3c artifact 绑定齐全（patch_digest+head_sha+finding_digest+context_digest）',
    d3.artifact.patch_digest === fa3?.patch_digest
      && d3.artifact.head_sha === s3.binding.headSha
      && d3.artifact.finding_digest?.length === 32
      && d3.artifact.context_digest === 'c'.repeat(32));

  // D4 状态推进 FIX_QUEUED→FIXING
  const st4 = (await pool.query(`SELECT status FROM mu.review_run WHERE run_id=$1`, [s3.run.run_id])).rows[0]?.status;
  ok('D4 run 推进到 FIXING', st4 === 'FIXING');

  // D5 库内零 patch 正文（只有 digest/artifact_ref）——代码不出审计面
  const cols5 = (await pool.query(
    `SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name='fix_attempt'`)).rows.map((r) => r.column_name);
  ok('D5 fix_attempt 无正文列（digest/ref only）', !cols5.includes('patch_text') && !cols5.includes('patch'));

  // D6 出站审计在（patch 生成调用入 code_egress_event——digest 零正文）
  const ev6 = (await pool.query(
    `SELECT count(*)::int c FROM mu.code_egress_event WHERE run_id=$1`, [s3.run.run_id])).rows[0].c;
  ok('D6 patch 生成调用入 egress 审计', ev6 >= 1);

  // D7 Provider HTTP 失败 → attempt FAILED retryable
  const s7 = await mkRun('FIX_QUEUED', true);
  const errFetch = async () => ({ ok: false, status: 503 });
  const d7 = await fx.runFixerSandbox(pool, { run: s7.run, binding: s7.binding,
    snapshot, findings: findingsFor(), context: { input_digest: 'g'.repeat(32) },
    deps: { fetchImpl: errFetch, baseUrl: 'https://mock', apiKey: 'k', model: 'm' } });
  ok('D7 Provider 503→FAILED retryable', d7.ok === false && d7.reason === 'FIXER_HTTP_503' && d7.retryable === true);
  const fa7 = (await pool.query(`SELECT count(*)::int c FROM mu.fix_attempt WHERE run_id=$1`, [s7.run.run_id])).rows[0].c;
  ok('D7b 失败不落 fix_attempt（零假 artifact）', fa7 === 0);

  // D8 新 head → 旧 DRY_RUN STALE（幂等）
  const st8 = await fx.markStalePatches(pool, { tenantId: T1, repoId: repo.repo_id,
    prId: s3.binding.prId, newHeadSha: 'newhead'.padEnd(40, '0') });
  ok('D8 新 head→旧 patch STALE', st8.staleCount === 1);
  const st8b = await fx.markStalePatches(pool, { tenantId: T1, repoId: repo.repo_id,
    prId: s3.binding.prId, newHeadSha: 'newhead'.padEnd(40, '0') });
  ok('D8b 幂等（重放零新增）', st8b.staleCount === 0);
  const fa8 = (await pool.query(`SELECT status FROM mu.fix_attempt WHERE run_id=$1`, [s3.run.run_id])).rows[0];
  ok('D8c 旧 patch 状态=STALE', fa8?.status === 'STALE');

  // D9 跨 PR 隔离：另一 PR 的 DRY_RUN 不受 markStalePatches 影响
  const s9 = await mkRun('FIX_QUEUED', true);
  const calls9 = [];
  await fx.runFixerSandbox(pool, { run: s9.run, binding: s9.binding,
    snapshot, findings: findingsFor(), context: { input_digest: 'h'.repeat(32) },
    deps: { fetchImpl: mkFetch(calls9), baseUrl: 'https://mock', apiKey: 'k', model: 'm' } });
  await fx.markStalePatches(pool, { tenantId: T1, repoId: repo.repo_id,
    prId: s3.binding.prId, newHeadSha: 'zzz'.padEnd(40, 'z') });
  const fa9 = (await pool.query(`SELECT status FROM mu.fix_attempt WHERE run_id=$1`, [s9.run.run_id])).rows[0];
  ok('D9 markStalePatches 只作用本 PR（他人 DRY_RUN 不动）', fa9?.status === 'DRY_RUN');

  // D10 mock 零真实网络（全部调用打向 mock baseUrl）
  const allCalls = [...calls1, ...calls2, ...calls3, ...calls9];
  ok('D10 零真实网络（所有 fetch 打向 mock）', allCalls.length > 0 && allCalls.every((u) => u.startsWith('https://mock/')));

  // D11 STALE 后同 run 再生成（FIXING 态可重跑）→ 新 artifact 仍 DRY_RUN
  const calls11 = [];
  const d11 = await fx.runFixerSandbox(pool, { run: s9.run, binding: s9.binding,
    snapshot, findings: findingsFor(), context: { input_digest: 'i'.repeat(32) },
    deps: { fetchImpl: mkFetch(calls11), baseUrl: 'https://mock', apiKey: 'k', model: 'm' } });
  ok('D11 FIXING 态可再生成（attempt=2）', d11.ok === true && d11.attemptId != null);
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
