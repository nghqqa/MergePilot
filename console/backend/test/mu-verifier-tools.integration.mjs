// console/backend/test/mu-verifier-tools.integration.mjs — PR E 门槛（mock Provider，零真实网络）。
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

const CTR = `mu-vfy-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17700 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;

const vf = await import('../lib/multiuser/agents/verifier-tools.mjs');
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

const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
const U = (await pool.query(`SELECT user_id FROM mu.app_user LIMIT 1`)).rows[0].user_id;
const CONSENT_D = crypto.createHash('sha256').update('c-text').digest('hex').slice(0, 32);

const repo = (await pool.query(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
  VALUES ($1,'github','vfy','arch','vfy') ON CONFLICT DO NOTHING RETURNING repo_id`, [T1])).rows[0]
  ?? (await pool.query(`SELECT repo_id FROM mu.repository WHERE tenant_id=$1 LIMIT 1`, [T1])).rows[0];

const FINDINGS = [{ rule_id: 'R-SECRET', severity: 'P0', path: 'a.js',
  line_start: 3, summary_masked: 'sk-***', confidence: 0.9 }];
const PATCH_TEXT = '--- a/a.js\n+++ b/a.js\n@@ -3,1 +3,1 @@\n-const k = "sk-realdanger";\n+const k = process.env.KEY;';

const mkPatchFetch = (calls) => async (url) => { calls.push(String(url));
  return { ok: true, status: 200, json: async () => ({ choices: [{ message: { content: PATCH_TEXT } }] }) }; };
const mkVerdictFetch = (verdict, calls) => async (url) => { calls?.push?.(String(url));
  return { ok: true, status: 200, json: async () => ({ choices: [{ message: {
    content: JSON.stringify({ verdict, note: 'patch addresses finding' }) } }] }) }; };

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

/** 全链装置：run→P0 finding→Leader fix_required→Fixer DRY_RUN→FIXING→VERIFY_QUEUED。 */
async function mkVerifiedReadyRun(snapshot) {
  const head = crypto.randomBytes(20).toString('hex');
  const pr = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,${Math.floor(Math.random() * 9000) + 100},$3) RETURNING pr_id`, [T1, repo.repo_id, head])).rows[0];
  const { run } = await orch.createRunIfAbsent(pool, { tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head });
  for (const [f, t] of [['RECEIVED','REVIEW_QUEUED'],['REVIEW_QUEUED','REVIEWING'],['REVIEWING','REVIEWED']]) {
    await orch.transitionRun(pool, { runId: run.run_id, from: [f], to: t });
  }
  const att = await orch.claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
    provider: 'deterministic', maxAttempts: 3, tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head });
  await orch.insertFindings(pool, { attemptId: att.attemptId, runId: run.run_id,
    tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head, findings: FINDINGS });
  await orch.finishAttempt(pool, { attemptId: att.attemptId, status: 'DONE' });
  await ext.leaderConsumeFindings(pool, { run, binding: { tenantId: T1,
    repoId: repo.repo_id, prId: pr.pr_id, headSha: head }, protection: { configured: false } });
  const binding = { tenantId: T1, repoId: repo.repo_id, prId: pr.pr_id, headSha: head };
  const fix = await fx.runFixerSandbox(pool, { run, binding, snapshot, findings: FINDINGS,
    context: { input_digest: 'f'.repeat(32) },
    deps: { fetchImpl: mkPatchFetch([]), baseUrl: 'https://mock', apiKey: 'k', model: 'deepseek-chat' } });
  if (!fix.ok) throw new Error('setup: fixer failed');
  await orch.transitionRun(pool, { runId: run.run_id, from: ['FIXING'], to: 'VERIFY_QUEUED' });
  return { run, binding, artifact: fix.artifact };
}

try {
  const snapshot = await externalSetup();

  // E1 输入缺失 fail-closed（无 artifact digest → 拒绝+零调用）
  const calls1 = [];
  const r1 = await vf.runVerifier(pool, { run: { run_id: crypto.randomUUID() }, binding: {
    tenantId: T1, repoId: repo.repo_id, prId: crypto.randomUUID(), headSha: 'h' },
    snapshot, findings: FINDINGS, patchArtifact: null,
    deps: { fetchImpl: mkVerdictFetch('PASS', calls1), baseUrl: 'https://mock', apiKey: 'k',
      model: 'm', patchText: PATCH_TEXT } });
  ok('E1 无 patch artifact→VERIFIER_INPUT_MISSING 零调用', r1.ok === false && r1.reason === 'VERIFIER_INPUT_MISSING' && calls1.length === 0);

  // E2 状态守卫：非 VERIFY_QUEUED/VERIFYING → 拒绝
  const s2 = await mkVerifiedReadyRun(snapshot);
  await orch.transitionRun(pool, { runId: s2.run.run_id, from: ['VERIFY_QUEUED'], to: 'VERIFYING' });
  await orch.transitionRun(pool, { runId: s2.run.run_id, from: ['VERIFYING'], to: 'VERIFIED' });
  const calls2 = [];
  const r2 = await vf.runVerifier(pool, { run: s2.run, binding: s2.binding, snapshot,
    findings: FINDINGS, patchArtifact: s2.artifact,
    deps: { fetchImpl: mkVerdictFetch('PASS', calls2), baseUrl: 'https://mock', apiKey: 'k',
      model: 'm', patchText: PATCH_TEXT } });
  ok('E2 非 VERIFY 阶段（VERIFIED）→ 拒绝零调用', r2.ok === false && r2.stage === 'not_verify_stage' && calls2.length === 0);

  // E3 artifact 不属于本 run（digest 不匹配）→ fail-closed
  const s3 = await mkVerifiedReadyRun(snapshot);
  const calls3 = [];
  const r3 = await vf.runVerifier(pool, { run: s3.run, binding: s3.binding, snapshot,
    findings: FINDINGS, patchArtifact: { patch_digest: 'x'.repeat(32) },
    deps: { fetchImpl: mkVerdictFetch('PASS', calls3), baseUrl: 'https://mock', apiKey: 'k',
      model: 'm', patchText: PATCH_TEXT } });
  ok('E3 patch_digest 非本 run→PATCH_NOT_OF_THIS_RUN', r3.ok === false && r3.reason === 'PATCH_NOT_OF_THIS_RUN' && calls3.length === 0);

  // E4 egress deny（evidence_only）→ 零网络
  const snapNo = arch.buildPolicySnapshot({ tenantId: T1,
    policy: { review_mode: 'evidence_only', policy_version: 1 } });
  const s4 = await mkVerifiedReadyRun(snapshot);
  const calls4 = [];
  const r4 = await vf.runVerifier(pool, { run: s4.run, binding: s4.binding, snapshot: snapNo,
    findings: FINDINGS, patchArtifact: s4.artifact,
    deps: { fetchImpl: mkVerdictFetch('PASS', calls4), baseUrl: 'https://mock', apiKey: 'k',
      model: 'm', patchText: PATCH_TEXT } });
  ok('E4 egress deny→零调用可重试', r4.ok === false && r4.stage === 'egress_denied' && calls4.length === 0);

  // E5 全链成功：model PASS + tools pass → 双绿→VERIFIED；双域分列
  const s5 = await mkVerifiedReadyRun(snapshot);
  const calls5 = [];
  const r5 = await vf.runVerifier(pool, { run: s5.run, binding: s5.binding, snapshot,
    findings: FINDINGS, patchArtifact: s5.artifact,
    deps: { fetchImpl: mkVerdictFetch('PASS', calls5), baseUrl: 'https://mock', apiKey: 'k',
      model: 'deepseek-chat', patchText: PATCH_TEXT } });
  ok('E5 Verifier 成功（model PASS + tools pass）', r5.ok === true);
  ok('E5b 双域分列（verification=passed ≠ 冒充 tests）',
    r5.verification_verdict === 'passed' && r5.tests_status === 'passed'
      && r5.attempt_verdict === 'PASS');
  const rr5 = (await pool.query(
    `SELECT verification_verdict, tests_status, status FROM mu.review_run WHERE run_id=$1`, [s5.run.run_id])).rows[0];
  ok('E5c run 列分立持久化+推进 VERIFIED',
    rr5.verification_verdict === 'passed' && rr5.tests_status === 'passed' && rr5.status === 'VERIFIED');
  const va5 = (await pool.query(
    `SELECT verdict, evidence_ref FROM mu.verification_attempt WHERE run_id=$1`, [s5.run.run_id])).rows[0];
  ok('E5d verification_attempt verdict=PASS+工具证据入 evidence_ref',
    va5?.verdict === 'PASS' && /static_check=ok/.test(va5?.evidence_ref ?? '') && /secret_scan=ok/.test(va5?.evidence_ref ?? ''));

  // E6 model PASS 但 tools FAIL → tests_status=failed（分列——模型不能盖过工具证据）
  const s6 = await mkVerifiedReadyRun(snapshot);
  const r6 = await vf.runVerifier(pool, { run: s6.run, binding: s6.binding, snapshot,
    findings: FINDINGS, patchArtifact: s6.artifact,
    deps: { fetchImpl: mkVerdictFetch('PASS', []), baseUrl: 'https://mock', apiKey: 'k',
      model: 'm', patchText: PATCH_TEXT + '\n+const password = "sample-not-real-secret";' } });
  ok('E6 model PASS+tools FAIL→分列不冒充',
    r6.ok === true && r6.verification_verdict === 'passed' && r6.tests_status === 'failed');
  const rr6 = (await pool.query(`SELECT tests_status, status FROM mu.review_run WHERE run_id=$1`, [s6.run.run_id])).rows[0];
  ok('E6b tests_status=failed 持久化+不推进 VERIFIED',
    rr6?.tests_status === 'failed' && rr6?.status === 'VERIFYING');

  // E7 模型不可用（HTTP 503）→ INCONCLUSIVE 落行+attempt FAILED——不冒充 PASS
  const s7 = await mkVerifiedReadyRun(snapshot);
  const r7 = await vf.runVerifier(pool, { run: s7.run, binding: s7.binding, snapshot,
    findings: FINDINGS, patchArtifact: s7.artifact,
    deps: { fetchImpl: async () => ({ ok: false, status: 503 }), baseUrl: 'https://mock',
      apiKey: 'k', model: 'm', patchText: PATCH_TEXT } });
  ok('E7 模型 503→INCONCLUSIVE+ok=false', r7.ok === false && r7.attempt_verdict === 'INCONCLUSIVE');
  const va7 = (await pool.query(
    `SELECT verdict, error_code FROM mu.verification_attempt WHERE run_id=$1`, [s7.run.run_id])).rows[0];
  ok('E7b INCONCLUSIVE 落行可审计（error_code=HTTP_503）',
    va7?.verdict === 'INCONCLUSIVE' && va7?.error_code === 'VERIFIER_HTTP_503');
  const rr7 = (await pool.query(
    `SELECT verification_verdict, tests_status, status FROM mu.review_run WHERE run_id=$1`, [s7.run.run_id])).rows[0];
  ok('E7c run=inconclusive（不冒充）+tools 证据仍留', rr7?.verification_verdict === 'inconclusive' && rr7?.tests_status === 'passed');
  const att7 = (await pool.query(
    `SELECT status, error_code FROM mu.agent_attempt WHERE run_id=$1 AND agent_role='verifier'`, [s7.run.run_id])).rows[0];
  ok('E7d verifier attempt FAILED（模型域失败独立记）', att7?.status === 'FAILED' && att7?.error_code === 'VERIFIER_HTTP_503');

  // E8 模型 FAIL → verification_verdict=failed
  const s8 = await mkVerifiedReadyRun(snapshot);
  const r8 = await vf.runVerifier(pool, { run: s8.run, binding: s8.binding, snapshot,
    findings: FINDINGS, patchArtifact: s8.artifact,
    deps: { fetchImpl: mkVerdictFetch('FAIL', []), baseUrl: 'https://mock', apiKey: 'k',
      model: 'm', patchText: PATCH_TEXT } });
  ok('E8 模型 FAIL→failed', r8.verification_verdict === 'failed');

  // E9 独立 verifier attempt（不复用 reviewer/fixer 会话）
  const attRoles = (await pool.query(
    `SELECT agent_role, provider FROM mu.agent_attempt WHERE run_id=$1 ORDER BY created_at`, [s5.run.run_id])).rows;
  ok('E9 独立 verifier attempt（provider=external_api）',
    attRoles.some((a) => a.agent_role === 'verifier' && a.provider === 'external_api')
      && attRoles.some((a) => a.agent_role === 'fixer'));

  // E10 出站审计在（model_judgment 调用入 code_egress_event）
  const ev10 = (await pool.query(
    `SELECT count(*)::int c FROM mu.code_egress_event e
      JOIN mu.agent_attempt a ON a.attempt_id = e.attempt_id
      WHERE e.run_id=$1 AND a.agent_role='verifier'`, [s5.run.run_id])).rows[0].c;
  ok('E10 verifier 调用入 egress 审计', ev10 >= 1);

  // E11 consent 撤销 → 即时 deny（撤销发生在装置就绪之后——fix 阶段 consent 仍有效）
  const s11 = await mkVerifiedReadyRun(snapshot);
  await rpStore.revokeConsent(T1, U, 'deepseek');
  const r11 = await vf.runVerifier(pool, { run: s11.run, binding: s11.binding, snapshot,
    findings: FINDINGS, patchArtifact: s11.artifact,
    deps: { fetchImpl: mkVerdictFetch('PASS', []), baseUrl: 'https://mock', apiKey: 'k',
      model: 'm', patchText: PATCH_TEXT } });
  ok('E11 consent 撤销→EGRESS_CONSENT_REVOKED', r11.ok === false && r11.reason === 'EGRESS_CONSENT_REVOKED');

  // E12 工具白名单冻结（无任意执行面）
  ok('E12 工具白名单=static_check+secret_scan（无 shell/exec）',
    vf.VERIFIER_TOOLS_ALLOWLIST.length === 2
      && vf.VERIFIER_TOOLS_ALLOWLIST.includes('static_check')
      && vf.VERIFIER_TOOLS_ALLOWLIST.includes('secret_scan'));

  // E13 单元级：staticCheck 危险模式命中/secretScan 命中
  ok('E13a staticCheck 捕获 eval', vf.staticCheck('x = eval(userInput)').passed === false);
  ok('E13b secretScan 捕获 AKIA', vf.secretScan('aws_key = AKIAIOSFODNN7EXAMPLE').passed === false);
  ok('E13c 干净 patch 双过', vf.staticCheck(PATCH_TEXT).passed === true && vf.secretScan(PATCH_TEXT).passed === true);
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
