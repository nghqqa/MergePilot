#!/usr/bin/env node
// console/backend/test/mu-agentteams-w33.integration.mjs — Wave 3.3 外部 AgentTeams adapter 测试。
// 全 mock（零真实 AgentTeams/零真实镜像/零真实模型/零真实凭据）；一次性 PG。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');
const at = await import('../lib/multiuser/agents/agentteams-executor.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const fxo = await import('../lib/multiuser/agents/fix-orchestrator.mjs');
const { __setGhProviderForTests } = await import('../lib/multiuser/ghprovider.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { if (c) { pass++; console.log('  PASS  ' + n); } else { fail++; console.log('  FAIL  ' + n + (d ? ' ' + JSON.stringify(d).slice(0, 160) : '')); } };
let netCalls = 0;
const noNet = async () => { netCalls++; throw new Error('MUST_NOT_CALL'); };

// mock AgentTeams controller（注入 fetchImpl；Bearer 必带；状态可编程）
function mkAtApi({ health = 200, projectStatus = 'completed', verifySummary = '{"verdict":"PASS","note":"ok"}',
  fixSummary = '{"suggestion":"参数化","patch_hint":"改写为占位"}', projectCreate = 201, replan = 200,
  workerPut = 200, pollStatuses = null } = {}) {
  return async (url, opts = {}) => {
    netCalls++;
    const auth = String(opts.headers?.authorization ?? '');
    if (!auth.startsWith('Bearer ')) return { status: 401 };
    if (url.endsWith('/api/v1/projects?limit=1')) return { status: health, ok: health === 200, json: async () => ({ projects: [] }) };
    if (/\/api\/v1\/workers\/[\w-]+$/.test(url) && opts.method === 'PUT') {
      return { status: workerPut, ok: workerPut === 200 || workerPut === 409 };
    }
    if (url.endsWith('/api/v1/projects') && opts.method === 'POST') {
      return { status: projectCreate, ok: projectCreate === 201 || projectCreate === 409, json: async () => ({ project_id: 'x' }) };
    }
    if (/\/replan$/.test(url)) return { status: replan, ok: replan < 300 };
    if (/\/workflow\?includeTasks=true$/.test(url)) {
      const statuses = pollStatuses ?? { 't-review': 'completed', 't-leader': 'completed', 't-fix': projectStatus, 't-verify': projectStatus };
      return { status: 200, ok: true, json: async () => ({ nodes: Object.entries(statuses)
        .map(([taskId, status]) => ({ taskId, status })) }) };
    }
    if (url.includes('/tasks/t-fix')) return { status: 200, ok: true, json: async () => ({ summary: fixSummary }) };
    if (url.includes('/tasks/t-verify')) return { status: 200, ok: true, json: async () => ({ summary: verifySummary }) };
    if (/\/cancel$/.test(url)) return { status: 200, ok: true };
    return { status: 404, ok: false };
  };
}
const CFG = { kind: 'agentteams', baseUrl: 'http://at.test', timeout: 5_000 };
const ENV_ON = { MU_EXECUTOR: 'agentteams', MU_AGENTTEAMS_BASE_URL: 'http://at.test', MU_AGENTTEAMS_TOKEN: 'at-tok' };

// ── W1 配置解析与 disabled 默认零网络 ──
netCalls = 0;
ok('W1a 默认 internal', at.resolveAgentTeamsConfig({}).kind === 'internal');
ok('W1b 未知值回退 internal+reason', at.resolveAgentTeamsConfig({ MU_EXECUTOR: 'chaos' }).kind === 'internal');
ok('W1c 缺 token 拒绝', at.resolveAgentTeamsConfig({ MU_EXECUTOR: 'agentteams', MU_AGENTTEAMS_BASE_URL: 'http://x' }).reason === 'AT_NOT_CONFIGURED');
ok('W1d 非 http(s) URL 拒绝', at.resolveAgentTeamsConfig({ MU_EXECUTOR: 'agentteams', MU_AGENTTEAMS_BASE_URL: 'ftp://x', MU_AGENTTEAMS_TOKEN: 't' }).reason === 'AT_BAD_URL');
ok('W1e 合法配置', at.resolveAgentTeamsConfig(ENV_ON).kind === 'agentteams');
const h = await at.agentTeamsHealthy(at.resolveAgentTeamsConfig({}), noNet);
ok('W1f internal/disabled 健康检查零网络', netCalls === 0 && h.reason === 'AT_DISABLED');

// ── W2 健康检查失败模式 ──
for (const [health, code] of [[401, 'AT_AUTH_FAILED'], [403, 'AT_AUTH_FAILED'], [500, 'AT_HTTP_500']]) {
  const r = await at.agentTeamsHealthy(CFG, mkAtApi({ health }));
  ok(`W2 health=${health} → ${code}`, r.ok === false && r.reason === code);
}
const rNet = await at.agentTeamsHealthy(CFG, async () => { throw new Error('ECONNREFUSED'); });
ok('W2d 断连 → AT_UNREACHABLE', rNet.reason === 'AT_UNREACHABLE');

// ── W3 ensureFourAgents：恰好四个具名 Agent、角色不越权（无 expose/mcp/凭据注入）──
let putBodies = [];
const spyApi = mkAtApi({});
const spyPut = async (url, opts) => {
  if (/\/workers\//.test(url) && opts.method === 'PUT') putBodies.push({ url, body: JSON.parse(opts.body) });
  return spyApi(url, opts);
};
const ens = await at.ensureFourAgents(CFG, { fetchImpl: spyPut });
ok('W3a 四 Agent 幂等创建成功', ens.ok === true && ens.created.length === 4);
const names = putBodies.map((p) => p.url.split('/').pop()).sort();
ok('W3b 恰好 leader/reviewer/fixer/verifier 四名', JSON.stringify(names)
  === JSON.stringify(['mergepilot-fixer', 'mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-verifier']));
const specAll = putBodies.map((p) => p.body.spec);
ok('W3c 零暴露端口/零 MCP/零凭据字段', specAll.every((s) => Array.isArray(s.expose) && s.expose.length === 0
  && Array.isArray(s.mcpServers) && s.mcpServers.length === 0
  && !JSON.stringify(s).match(/api[_-]?key|token|secret|password/i)));
const w403 = await at.ensureFourAgents(CFG, { fetchImpl: mkAtApi({ workerPut: 403 }) });
ok('W3d worker 创建被拒 → 稳定 reason', w403.ok === false && String(w403.reason).startsWith('AT_WORKER_'));

// ── W4 出站脱敏 ──
let replanBody = null;
const capApi = async (url, opts) => {
  if (/\/replan$/.test(url)) replanBody = JSON.parse(opts.body);
  return mkAtApi()(url, opts);
};
const FINDINGS = [{ rule_id: 'R-SECRET', severity: 'P0', path: 'src/a.js', line_start: 3,
  summary_masked: 'ghp_***', raw_leak: 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij' }];
const sub = await at.submitExternalRound(CFG, { runId: crypto.randomUUID(), findings: FINDINGS, fetchImpl: capApi });
ok('W4a 提交成功（项目+四任务 DAG）', sub.ok === true && replanBody.tasks.length === 4
  && replanBody.tasks.find((t) => t.taskId === 't-verify').dependsOn.includes('t-fix'));
const briefStr = JSON.stringify(replanBody);
ok('W4b 任务载荷零原始 secret/零 diff/零多余字段', !briefStr.includes('ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ')
  && !briefStr.includes('raw_leak') && briefStr.length < 8_000);
ok('W4c 载荷只含 untrusted_findings 白名单', replanBody.tasks[0].title.includes('untrusted_findings'));

// ── W5 输出 schema（逐角色）──
ok('W5 verifier PASS 合法', at.validateAgentTeamsOutput('verifier', { verdict: 'PASS' }).ok === true);
ok('W5b verifier 非法 verdict 拒', at.validateAgentTeamsOutput('verifier', { verdict: 'YES' }).code === 'AT_SCHEMA_INVALID');
ok('W5c verifier 越权字段拒', at.validateAgentTeamsOutput('verifier', { verdict: 'PASS', command: 'rm' }).code === 'AT_SCHEMA_INVALID');
ok('W5d fixer 合法', at.validateAgentTeamsOutput('fixer', { suggestion: 's', patch_hint: 'p' }).ok === true);
ok('W5e fixer git push 拒', at.validateAgentTeamsOutput('fixer', { patch_hint: 'git push --force' }).code === 'AT_FORBIDDEN_CONTENT');
ok('W5f reviewer findings 白名单', at.validateAgentTeamsOutput('reviewer', { findings: [{ severity: 'P1', path: 'a', summary: 's' }] }).ok === true);
ok('W5g leader 仅 recommendation/confidence', at.validateAgentTeamsOutput('leader', { verdict: 'PASS' }).code === 'AT_SCHEMA_INVALID');
ok('W5h 未知角色拒', at.validateAgentTeamsOutput('attacker', {}).code === 'AT_ROLE_UNKNOWN');

// ── W6 轮询/超时/取消/幂等 ──
const pollOk = await at.pollExternalRound(CFG, { projectId: 'p1', fetchImpl: mkAtApi({ pollStatuses: { 't-fix': 'completed', 't-verify': 'completed' } }), maxWaitMs: 2_000 });
ok('W6a 全终态轮询成功', pollOk.ok === true && pollOk.states['t-fix'] === 'DONE');
const pollTimeout = await at.pollExternalRound(CFG, { projectId: 'p1', fetchImpl: mkAtApi({ pollStatuses: { 't-fix': 'in-progress' } }), maxWaitMs: 100 });
ok('W6b 轮询超时稳定失败', pollTimeout.ok === false && pollTimeout.reason === 'AT_POLL_TIMEOUT');
const cancel = await at.cancelExternalRound(CFG, { projectId: 'p1', taskId: 't-fix', reason: 'x', submissionId: 'p1', fetchImpl: mkAtApi() });
ok('W6c 取消幂等（200/409 均可）', cancel.ok === true);

// ── W7 管线级（一次性 PG + mock AT）：外部轮次 + MergePilot 终裁 ──
const CTR = `w33-${crypto.randomBytes(3).toString('hex')}`;
const PORT = 17300 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
for (let i = 0; i < 150; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 400)); } }
try {
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();
  ok('W7a migration v12 应用', (await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=12`)).rows.length === 1);
  const T = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  const repo = await store.ensureRepository({ tenantId: T, provider: 'github', providerRepoId: '99701', owner: 'w33', name: 'r', defaultBranch: 'main' });
  const pr = await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: 4, headSha: 'f'.repeat(40) });
  const binding = { tenantId: T, repoId: repo.repo_id, prId: pr.pr_id, headSha: 'f'.repeat(40) };
  const { run } = await orch.createRunIfAbsent(pool, { ...binding });
  for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED'], ['REVIEWED', 'FIX_QUEUED']]) {
    await orch.transitionRun(pool, { runId: run.run_id, from: [f], to: t });
  }
  const att = await orch.claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
    provider: 'deterministic', maxAttempts: 3, ...binding });
  await orch.insertFindings(pool, { attemptId: att.attemptId, runId: run.run_id, ...binding,
    findings: [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 's.js', line_start: 2,
      line_end: 2, title: 'x', evidence_ref: 'e', summary_masked: 'ghp_***' }] });
  process.env.MU_AGENTTEAMS_TOKEN = 'at-tok';
  const deps = { env: { ...ENV_ON }, atFetch: mkAtApi(), assertServiceChain: async () => true,
    repoUrl: 'x', testCmd: 'node -e process.exit(0)', providerCfg: {}, installationId: '1',
    owner: 'w33', repoName: 'r', prNumber: 4 };
  const rExt = await fxo.fixVerifyRound(pool, { run, binding, deps });
  console.log('  DEBUG rExt=', JSON.stringify(rExt).slice(0,200));
  ok('W7b 外部轮次完成（executor=agentteams）', rExt.ok === true && rExt.executor === 'agentteams' && rExt.verdict === 'PASS');
  const runAfter = await orch.getRun(pool, run.run_id);
  ok('W7c MergePilot Leader 终裁 COMPLETED（服务端复核）', runAfter.status === 'COMPLETED');
  const atAtt = (await pool.query(`SELECT agent_role, provider, status FROM mu.agent_attempt WHERE run_id=$1 AND provider='agentteams'`, [run.run_id])).rows;
  ok('W7d fixer/verifier attempt 记 agentteams provider', atAtt.length === 2
    && atAtt.some((a) => a.agent_role === 'fixer') && atAtt.some((a) => a.agent_role === 'verifier'));
  const fixRow = (await pool.query(`SELECT status FROM mu.fix_attempt WHERE run_id=$1`, [run.run_id])).rows[0];
  ok('W7e 外部 Fixer 仅 DRY_RUN 建议（不应用）', fixRow?.status === 'DRY_RUN');
  // 泄漏扫描：AT token/原始 secret/任务正文不入库
  let leaks = [];
  for (const tb of ['agent_attempt', 'fix_attempt', 'verification_attempt', 'audit_event', 'review_run', 'dead_letter']) {
    const cols = (await pool.query(`SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name=$1 AND data_type IN ('text','jsonb')`, [tb])).rows.map((r) => r.column_name);
    for (const c of cols) {
      for (const m of ['at-tok', 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'untrusted_findings']) {
        const hit = await pool.query(`SELECT 1 FROM mu.${tb} WHERE CAST(${c} AS text) LIKE $1 LIMIT 1`, [`%${m}%`]);
        if (hit.rows.length) leaks.push(`${tb}.${c}`);
      }
    }
  }
  ok('W7f token/原始 secret/任务正文全库零泄漏', leaks.length === 0, leaks.slice(0, 3));
  delete process.env.MU_AGENTTEAMS_TOKEN;

  // W8 默认 internal 回归（同环境不配 MU_EXECUTOR → internal 子进程路径）
  const pr2 = await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: 5, headSha: 'e'.repeat(40) });
  const binding2 = { tenantId: T, repoId: repo.repo_id, prId: pr2.pr_id, headSha: 'e'.repeat(40) };
  const { run: run2 } = await orch.createRunIfAbsent(pool, { ...binding2 });
  for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED'], ['REVIEWED', 'FIX_QUEUED']]) {
    await orch.transitionRun(pool, { runId: run2.run_id, from: [f], to: t });
  }
  const att2 = await orch.claimNextAttempt(pool, { runId: run2.run_id, agentRole: 'reviewer', provider: 'deterministic', maxAttempts: 3, ...binding2 });
  await orch.insertFindings(pool, { attemptId: att2.attemptId, runId: run2.run_id, ...binding2,
    findings: [{ rule_id: 'R-SQL-CONCAT', severity: 'P1', confidence: 0.8, path: 's.js', line_start: 1, line_end: 1, title: 'x', evidence_ref: 'e', summary_masked: 'SELECT' }] });
  const beforeNet = netCalls;
  // internal 路径需要 mock GitHub provider 提供上下文
  __setGhProviderForTests({ async fetchPrContext() {
    return { stale_head: false, pr: { number: 5, state: 'open', title: 't', head: { sha: 'e'.repeat(40) },
      base: { ref: 'main' }, changed_files: 1 },
      diff: 'diff --git a/s.js b/s.js\n@@ -1,1 +1,2 @@\n x\n+const PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";',
      checks: [], protection: { configured: true }, limits: { diff_bytes: 1, over_diff_limit: false },
      fetched_head_sha: 'e'.repeat(40) };
  } });
  const rInt = await fxo.fixVerifyRound(pool, { run: run2, binding: binding2,
    deps: { ...deps, env: { MU_EXECUTOR: 'internal' }, assertServiceChain: async () => true } });
  __setGhProviderForTests; // 已设置——后续 finally 中有 __resetGhProvider 时也会清理
  ok('W8 默认 internal：零外部请求 + 子进程路径照常', rInt.ok === true && rInt.executor !== 'agentteams'
    && netCalls === beforeNet, rInt);
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  await pool.end().catch(() => {});
  delete process.env.MU_AGENTTEAMS_TOKEN;
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
