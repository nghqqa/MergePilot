// model-review.integration.test.mjs — 静态零发现时 LLM 模型审查管线贯通（一次性 PG + 本地 mock LLM server）。
// 覆盖：零静态发现→策略启用→真实出站路径（openai_compatible→本地 mock OpenAI）→
// LLM finding 正式落库（source=reviewer）→Leader 裁定（P1→WAITING 审批票）→
// 出站审计→幂等（重复消费不重复执行）→未授权零网络→越界/陈旧 head 拒绝。
import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..', '..');
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass += 1; console.log('  PASS ', name); }
  else { fail += 1; console.log('  FAIL ', name, JSON.stringify(detail ?? '').slice(0, 240)); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const reviewService = await import(pathToFileURL(path.join(ROOT, 'console', 'backend', 'lib', 'multiuser', 'review-service.mjs')));
const leader = await import(pathToFileURL(path.join(ROOT, 'console', 'backend', 'lib', 'multiuser', 'agents', 'leader.mjs')));

const HEAD = 'e790715149838916bee05da9d9f90e3d4db30c00'.slice(0, 40).padEnd(40, '0');
const DIFF = [
  'diff --git a/src/loop.js b/src/loop.js',
  '--- a/src/loop.js',
  '+++ b/src/loop.js',
  '@@ -1,4 +1,4 @@',
  ' function sumDiscounts(prices) {',
  '-for (let i = 0; i <= prices.length; i++) sum += prices[i];',
  '+for (let i = 0; i < prices.length; i++) sum += prices[i];',
  ' }',
].join('\n');

// ── 本地 mock OpenAI（记录请求；返回 P1 logic_bug finding）──
const llmHits = [];
const llmServer = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', () => {
    llmHits.push({ path: req.url, auth: Boolean(req.headers.authorization), bytes: body.length });
    const content = JSON.stringify({ findings: [{
      severity: 'P1', category: 'logic_bug',
      location: { path: 'src/loop.js', line: 2 },
      summary: 'off-by-one: loop bound reads one past end of array',
      recommendation: 'Use i < prices.length instead of i <= prices.length.',
      confidence: 0.92 }] });
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ choices: [{ message: { content } }] }));
  });
});
await new Promise((r) => llmServer.listen(0, '127.0.0.1', r));
const LLM_PORT = llmServer.address().port;

// ── 一次性 PG ──
const CTR = `mu-mrev-${crypto.randomBytes(3).toString('hex')}`;
const PGPORT = 17700 + Math.floor(Math.random() * 60);
try {
  const { execFileSync } = await import('node:child_process');
  execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  const { createMuStore } = await import(pathToFileURL(path.join(ROOT, 'console', 'backend', 'lib', 'multiuser', 'store.mjs')));
  const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PGPORT}/mu` });
  for (let i = 0; i < 150; i++) { try { await pool.query('SELECT 1'); break; } catch { await sleep(400); } }
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();
  const T = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  const REPO = crypto.randomUUID(); const PRID = crypto.randomUUID();
  const INSTANCE = 'gitee-cloud';

  // ── 种子：forge_connection+repository（forge JOIN 需要）+PR ──
  await pool.query(
    `INSERT INTO mu.forge_connection (connection_id, tenant_id, instance_id, credential_ref, webhook_mode, status)
     VALUES (gen_random_uuid(), $1, $2, 'env:MU_GITEE_PAT', 'signature', 'valid')`, [T, INSTANCE]);
  await pool.query(
    `INSERT INTO mu.repository (repo_id, tenant_id, provider, provider_repo_id, owner, name, forge_instance_id)
     VALUES ($1,$2,'gitee','50740631','nghqqa','mergepilot-test',$3)`, [REPO, T, INSTANCE]);
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,9,$4,'open')`, [PRID, T, REPO, HEAD]);

  // ── 策略与授权种子：agent_policy 启用 llm_assist；review_policy external_api；
  //    provider_registry（deepseek acknowledged）；provider_consent（code_egress=true）──
  await pool.query(
    `UPDATE mu.agent_policy SET enabled=true, mode='llm_assist', provider='openai_compatible',
       model='deepseek-chat', policy_version=policy_version+1 WHERE id=1`);
  await pool.query(
    `INSERT INTO mu.provider_registry (provider_id, display_name, policy_status)
     VALUES ('deepseek','DeepSeek','custom_acknowledged')
     ON CONFLICT (provider_id) DO UPDATE SET policy_status='custom_acknowledged'`).catch(() => {});
  await pool.query(
    `INSERT INTO mu.review_policy (tenant_id, review_mode, provider_id, model_id, policy_version, code_egress_allowed, consent_version)
     VALUES ($1,'external_api','deepseek','deepseek-chat',1, true, 'trial-v1')`, [T]);
  await pool.query(
    `INSERT INTO mu.provider_consent (consent_id, tenant_id, provider_id, consent_version,
       policy_version, accepted_at, accepted_by, acknowledgement_digest, code_egress_allowed)
     VALUES (nextval('mu.provider_consent_consent_id_seq'::regclass), $1, 'deepseek', 'trial-v1', 1, now(), (SELECT user_id FROM mu.app_user LIMIT 1), 'trial-digest', true)`, [T]);

  const llmEnv = {
    MU_LLM_PROVIDER: 'openai_compatible',
    MU_LLM_BASE_URL: `http://127.0.0.1:${LLM_PORT}`,
    MU_LLM_API_KEY: 'test-key-not-real',
    MU_LLM_MODEL: 'deepseek-chat',
    MU_LLM_ALLOWED_MODELS: 'deepseek-chat,deepseek-flash',
    MU_LLM_ALLOW_INSECURE_BASE_URL: '1',
  };
  const forgeAdapter = { fetchChangeContext: async () => ({
    diff: DIFF, stale_head: false, fetched_head_sha: HEAD,
    pr: { number: 9, title: 'off-by-one fixture', head: { sha: HEAD }, base: { ref: 'master' },
      changed_files: 1 },
    limits: null, completeness: { status: 'full', reason: 'test_fixture' },
    protection: { provided: false } }) };
  const cfg = { llmEnv, forgeAdapter };

  // ── 场景 1：静态零发现 + 策略启用 → LLM 执行 → finding 落库（source=reviewer）──
  const payload = { schema_version: 1, event: 'pull_request', forge_kind: 'gitee',
    delivery_ref: 'trial-mrev-1', provider_repo_id: '50740631', pr_number: 9,
    head_sha: HEAD, trigger_source: 'webhook' };
  const rv = await reviewService.handlePullRequestEvent(pool, cfg, { payload });
  ok('1a 零静态发现 run 完成', rv.ok === true && rv.findings_count === 1 && rv.llm_findings === 1,
    { findings_count: rv.findings_count, llm: rv.llm_findings, code: rv.llm_code });
  const att = (await pool.query(
    `SELECT provider, status FROM mu.agent_attempt WHERE run_id=$1 ORDER BY created_at`, [rv.run.run_id])).rows;
  ok('1b LLM attempt DONE（provider=openai_compatible）',
    att.some((a) => a.provider === 'openai_compatible' && a.status === 'DONE'), att);
  const fRow = (await pool.query(
    `SELECT rule_id, severity, source, path FROM mu.agent_finding WHERE run_id=$1`,
    [rv.run.run_id])).rows[0] ?? null;
  ok('1c LLM finding 落库 source=reviewer（不与静态混同）',
    fRow && fRow.rule_id === 'LLM-logic_bug' && fRow.severity === 'P1' && fRow.source === 'reviewer', fRow);
  const egress = (await pool.query(
    `SELECT count(*) c FROM mu.audit_event WHERE kind='REVIEW_LLM_EGRESS'`)).rows[0].c;
  ok('1d 出站审计 REVIEW_LLM_EGRESS', Number(egress) >= 1, { egress });

  // ── 场景 2：模型 P1 → Leader 裁定 → WAITING 审批票（零未授权修复）──
  const findings2 = (await pool.query(
    `SELECT rule_id, severity, path, line_start, title, summary_masked FROM mu.agent_finding
      WHERE run_id=$1 ORDER BY severity`, [rv.run.run_id])).rows;
  const dec = await leader.advanceAfterReview(pool, { runId: rv.run.run_id,
    tenantId: T, repoId: REPO, prId: rv.run.pr_id, headSha: HEAD, findings: findings2,
    protection: { provided: false } });
  ok('2a 模型 P1 → fix_required', dec.decision === 'fix_required', dec);
  const run2 = (await pool.query(`SELECT status FROM mu.review_run WHERE run_id=$1`, [rv.run.run_id])).rows[0];
  ok('2b run 进入 WAITING_FOR_HUMAN_APPROVAL（未批准不执行修复）',
    run2.status === 'WAITING_FOR_HUMAN_APPROVAL', run2);
  const ticket = (await pool.query(
    `SELECT approval_id, status, severity, run_id::text FROM mu.fix_approval WHERE run_id=$1`,
    [rv.run.run_id])).rows[0] ?? null;
  ok('2c 审批票生成且绑定同一 run（P1 与 finding 一致）',
    ticket && ticket.status === 'PENDING' && ticket.severity === 'P1' && ticket.run_id === rv.run.run_id, ticket);

  // ── 场景 3：重复消费 → 幂等（不重复执行 LLM/落库）──
  const llmHitsBefore = llmHits.length;
  const rv2 = await reviewService.handlePullRequestEvent(pool, cfg, { payload });
  ok('3a 重复消费 idempotent', rv2.idempotent === true, rv2);
  ok('3b LLM 调用不重复', llmHits.length === llmHitsBefore, { before: llmHitsBefore, after: llmHits.length });

  // ── 场景 4：未授权（policy disabled）→ 零 LLM 网络+原因记录 ──
  await pool.query(`UPDATE mu.agent_policy SET enabled=false, mode='deterministic_only' WHERE id=1`);
  const payloadOff = { ...payload, delivery_ref: 'trial-mrev-2', head_sha: HEAD.slice(0, 39) + '1' };
  const pr4 = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,10,$4,'open')`, [pr4, T, REPO, payloadOff.head_sha]);
  // PR 快照（resolveForgeServiceContext 按 provider_pr_number 最新）——改指 PR10
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES (gen_random_uuid(), $1,$2,10,$3,'open') ON CONFLICT DO NOTHING`, [T, REPO, payloadOff.head_sha]);
  const rvOff = await reviewService.handlePullRequestEvent(pool, cfg, { payload: payloadOff });
  const offAtt = (await pool.query(
    `SELECT count(*) c FROM mu.agent_attempt a JOIN mu.review_run r ON r.run_id=a.run_id
      WHERE r.head_sha=$1 AND a.provider='openai_compatible'`, [payloadOff.head_sha])).rows[0].c;
  ok('4a 未授权：零 LLM 网络', Number(offAtt) === 0 && llmHits.length === llmHitsBefore, { offAtt });
  ok('4b 未授权原因如实（llm_mode=deterministic_only 快照）',
    rvOff.ok === true && rvOff.run.llm_mode === 'deterministic_only', rvOff.run);

  // ── 场景 5：陈旧 head → attempt SKIPPED+BLOCKED（TOCTOU 防护无回归）──
  const stalePr = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,11,$4,'open')`, [stalePr, T, REPO, HEAD]);
  const staleAdapter = { fetchChangeContext: async () => ({ stale_head: true }) };
  const rvStale = await reviewService.handlePullRequestEvent(pool,
    { ...cfg, forgeAdapter: staleAdapter },
    { payload: { ...payload, delivery_ref: 'trial-mrev-3', pr_number: 11 },
      servicePrincipal: 'system:webhook' });
  ok('5 陈旧 head 拒绝（stale_head）', rvStale.ok === false && rvStale.reason === 'stale_head', rvStale);

  await pool.end();
} catch (e) {
  fail += 1;
  console.log('  FAIL 集成异常:', String(e?.stack ?? e).slice(0, 500));
} finally {
  try {
    const { execFileSync } = await import('node:child_process');
    execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' });
  } catch { /* 尽力清理 */ }
  llmServer.close();
}

console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);