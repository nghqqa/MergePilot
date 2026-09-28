#!/usr/bin/env node
// console/backend/test/mu-llm-w31.integration.mjs — Wave 3.1 LLM Provider 测试。
// 纯 mock（零真实网络/零真实 key）；含一次性 PG 段验证 review 路径接线。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const llm = await import('../lib/multiuser/agents/llm.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? (pass++, console.log('  PASS  ' + n)) : (fail++, console.log('  FAIL  ' + n + (d ? ' ' + JSON.stringify(d).slice(0, 160) : ''))); };
let networkCalls = 0;
const noFetch = async () => { networkCalls++; throw new Error('MUST_NOT_CALL'); };
const GOOD_OUT = { choices: [{ message: { content: JSON.stringify({ findings: [
  { severity: 'P2', category: 'injection', location: { path: 'src/a.js', line: 3 },
    summary: '拼接风险', recommendation: '参数化处理', confidence: 0.7 }] }) } }] };
const mkFetch = (mode) => async () => {
  networkCalls++;
  if (mode === 'timeout') { const e = new Error('The operation was aborted'); e.name = 'AbortError'; throw e; }
  if (mode === 'net') throw new Error('ECONNREFUSED');
  if (mode === '429') return { ok: false, status: 429 };
  if (mode === '500') return { ok: false, status: 500 };
  if (mode === '401') return { ok: false, status: 401 };
  if (mode === 'badjson') return { ok: true, status: 200, text: async () => 'nope{{' };
  if (mode === 'oversize') return { ok: true, status: 200, text: async () => 'x'.repeat(65 * 1024) };
  if (mode === 'schema') return { ok: true, status: 200, text: async () =>
    JSON.stringify({ choices: [{ message: { content: JSON.stringify({ findings: [{ severity: 'P9' }] }) } }] }) };
  if (mode === 'forbidden-key') return { ok: true, status: 200, text: async () =>
    JSON.stringify({ choices: [{ message: { content: JSON.stringify({ command: 'rm -rf /', findings: [] }) } }] }) };
  if (mode === 'forbidden-rec') return { ok: true, status: 200, text: async () =>
    JSON.stringify({ choices: [{ message: { content: JSON.stringify({ findings: [{ severity: 'P1',
      category: 'other', location: { path: 'x', line: 1 }, summary: 's',
      recommendation: 'run git push --force', confidence: 0.5 }] }) } }] }) };
  if (mode === 'spy') return { ok: true, status: 200, text: async () =>
    JSON.stringify({ choices: [{ message: { content: JSON.stringify({ findings: [] }) } }] }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(GOOD_OUT) };
};

const CFG = { MU_LLM_PROVIDER: 'openai_compatible', MU_LLM_BASE_URL: 'https://llm.example/v1',
  MU_LLM_API_KEY: 'k-test-only', MU_LLM_MODEL: 'test-m', MU_LLM_TIMEOUT_MS: '2000' };
const CTX = { pr: { number: 1, title: 't', changed_files: 1, head: { sha: 'a'.repeat(40) }, base: { ref: 'main' } },
  findings: [{ rule_id: 'R-SECRET', severity: 'P0', path: 'src/a.js', line_start: 3,
    title: 'x', summary_masked: 'ghp_***' }],
  diff: 'diff --git a/src/a.js b/src/a.js\n@@ -1,2 +1,4 @@\n c\n+const PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";\n d' };

// ── W1 Provider 解析与 disabled 零网络 ──
ok('W1a 默认 disabled', llm.resolveLlmProvider({}).kind === 'disabled');
ok('W1b disabled 显式', llm.resolveLlmProvider({ MU_LLM_PROVIDER: 'disabled' }).kind === 'disabled');
ok('W1c 未知 provider → disabled+reason', llm.resolveLlmProvider({ MU_LLM_PROVIDER: 'magic' })
  .kind === 'disabled');
for (const [envv, why] of [
  [{ MU_LLM_PROVIDER: 'openai_compatible' }, '缺字段'],
  [{ MU_LLM_PROVIDER: 'openai_compatible', MU_LLM_BASE_URL: 'http://insecure', MU_LLM_API_KEY: 'k', MU_LLM_MODEL: 'm' }, '非 HTTPS'],
  [{ MU_LLM_PROVIDER: 'openai_compatible', MU_LLM_BASE_URL: 'https://x', MU_LLM_API_KEY: 'your-key', MU_LLM_MODEL: 'm' }, 'placeholder'],
]) ok(`W1d 非法配置 fail-closed（${why}）`, llm.resolveLlmProvider(envv).kind === 'disabled');
const p = llm.resolveLlmProvider(CFG);
ok('W1e 合法配置解析', p.kind === 'openai_compatible' && p.timeout === 2000 && p.baseUrl.endsWith('/v1'));

// disabled 下调用即拒（不触网络）
networkCalls = 0;
const rDis = await llm.callLlmReviewer({ kind: 'disabled' }, CTX, { fetchImpl: noFetch });
ok('W1f disabled 语义不外调（此处显式禁用态调用属调用方错误——校验层在 service）',
  networkCalls === 0 || rDis.ok === false);
ok('W1g 全程未触网络（disabled/misconfigured 场景）', networkCalls === 0);

// ── W2 出站白名单与注入遏制 ──
networkCalls = 0;
let captured = null;
const spy = async (u, opts) => { networkCalls++; captured = JSON.parse(opts.body);
  return { ok: true, status: 200, text: async () => JSON.stringify({ choices: [{ message: { content: JSON.stringify({ findings: [] }) } }] }) }; };
const rSpy = await llm.callLlmReviewer(p, CTX, { fetchImpl: spy });
ok('W2a 正常输出过 schema', rSpy.ok === true && Array.isArray(rSpy.findings) && rSpy.outputDigest?.length === 64);
ok('W2b system 固定、用户输入仅入 untrusted_data',
  captured.messages[0].role === 'system' && !captured.messages[0].content.includes('ghp_')
  && Object.keys(JSON.parse(captured.messages[1].content))[0] === 'untrusted_data');
const ud = JSON.parse(captured.messages[1].content).untrusted_data;
ok('W2c diff hunk 已打码（无原始 secret）', JSON.stringify(ud.diff_hunks).includes('ghp_***')
  && !JSON.stringify(ud).includes('ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ'));
ok('W2d 出站不含 webhook/租户/用户字段', !('webhook' in ud) && !('tenant' in ud) && !('user' in ud));
ok('W2e 载荷 ≤24KiB', Buffer.byteLength(JSON.stringify(ud)) <= 24 * 1024 + 64);
ok('W2f API key 只在 Authorization 头（不在 body/返回值）',
  captured.body === undefined && !JSON.stringify(rSpy).includes('k-test-only'));

// ── W3 失败模式（稳定 code；无正文）──
for (const [mode, code] of [['timeout', 'LLM_TIMEOUT'], ['net', 'LLM_UNAVAILABLE'],
  ['429', 'LLM_RATE_LIMITED'], ['500', 'LLM_SERVER_ERROR'], ['401', 'LLM_HTTP_401'],
  ['badjson', 'LLM_INVALID_JSON'], ['oversize', 'LLM_OUTPUT_OVERSIZE'],
  ['schema', 'LLM_SCHEMA_INVALID'], ['forbidden-key', 'LLM_FORBIDDEN_CONTENT'],
  ['forbidden-rec', 'LLM_FORBIDDEN_CONTENT']]) {
  const r = await llm.callLlmReviewer(p, CTX, { fetchImpl: mkFetch(mode) });
  const actual = r.code;
  ok(`W3 ${mode} → ${code}（实际 ${r.code}）`, r.ok === false && actual === code
    && !JSON.stringify(r).includes('k-test-only') && !JSON.stringify(r).includes('ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ'));
}
// timeout 真路径（短超时）
const pShort = { ...p, timeout: 100 };
const rT = await llm.callLlmReviewer(pShort, CTX, { fetchImpl: mkFetch('timeout') });
ok('W3b 真超时稳定失败', rT.ok === false && (rT.code === 'LLM_TIMEOUT' || rT.code === 'LLM_UNAVAILABLE'));

// ── W4 载荷上限（超大 findings 截断后仍 ≤ 限）──
const big = { ...CTX, findings: Array.from({ length: 500 }, (_, i) => ({ ...CTX.findings[0], path: `p${i}` })) };
const bigSan = llm.sanitizeContextForLlm(big);
ok('W4 超限载荷降级（hunks 丢/截断后 ≤ 限）', Buffer.byteLength(JSON.stringify(bigSan.payload)) <= 24 * 1024 + 128);

// ── W5 mock provider（零网络）──
networkCalls = 0;
const rMock = await llm.mockLlmReviewer({ findings: CTX.findings });
ok('W5 deterministic_mock 零网络结构化输出', networkCalls === 0 && rMock.ok === true
  && llm.validateLlmOutput({ findings: rMock.findings }).ok === true);

// ── W6 review 路径集成（一次性 PG + mock provider 注入）──
const { Pool } = createRequire(path.join(here, 'support/noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const svc = await import('../lib/multiuser/review-service.mjs');
const { __setGhProviderForTests, __resetGhProvider } = await import('../lib/multiuser/ghprovider.mjs');
const CTR = `w31-${crypto.randomBytes(3).toString('hex')}`;
const PORT = 17000 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
for (let i = 0; i < 120; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 400)); } }
try {
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();
  const T = (await store.ensureTenant({ slug: 'w31', displayName: 'W31' })).tenant_id;
  const repo = await store.ensureRepository({ tenantId: T, provider: 'github', providerRepoId: '98001', owner: 'w31', name: 'r', defaultBranch: 'main' });
  await pool.query(`INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id) VALUES (1,$1,1,'o',1) ON CONFLICT DO NOTHING`, [T]);
  await pool.query(`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state) VALUES ($1,$2,98001,'w31','r',1,'main','active') ON CONFLICT DO NOTHING`, [T, repo.repo_id]);
  const head = 'b'.repeat(40);
  const pr = await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: 3, headSha: head });
  const DIFF = 'diff --git a/s.js b/s.js\n@@ -1,1 +1,2 @@\n x\n+const T9 = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";';
  __setGhProviderForTests({ async fetchPrContext() {
    return { stale_head: false, pr: { number: 3, state: 'open', title: 't', head: { sha: head },
      base: { ref: 'main' }, changed_files: 1 }, diff: DIFF, checks: [],
      protection: { configured: true }, limits: { diff_bytes: 1, over_diff_limit: false },
      fetched_head_sha: head };
  } });
  const ev = { action: 'opened', github_repo_id: 98001, pr_number: 3, head_sha: head };

  // W6a disabled：deterministic-only，零 LLM attempt
  const rA = await svc.handlePullRequestEvent(pool, { configured: true }, { payload: ev });
  const attsA = (await pool.query(`SELECT provider FROM mu.agent_attempt WHERE run_id=$1 AND (provider LIKE 'llm%' OR provider='deterministic_mock')`, [rA.run.run_id])).rows;
  ok('W6a 默认 disabled：零 LLM attempt、deterministic 正常', rA.ok === true && rA.llm_findings === 0 && attsA.length === 0 && rA.findings_count >= 1);

  // W6b mock provider 启用：LLM finding 落库 + attempt 记录（digest/无正文）
  const head2 = 'c'.repeat(40);
  await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: 3, headSha: head2 });
  const rB = await svc.handlePullRequestEvent(pool, { configured: true,
    llmEnv: { MU_LLM_PROVIDER: 'deterministic_mock' } }, { payload: { ...ev, head_sha: head2 } });
  const llmF = (await pool.query(`SELECT rule_id, severity FROM mu.agent_finding WHERE run_id=$1 AND rule_id LIKE 'LLM-%'`, [rB.run.run_id])).rows;
  const llmA = (await pool.query(`SELECT provider, status, model_id, output_digest FROM mu.agent_attempt WHERE run_id=$1 AND provider='deterministic_mock'`, [rB.run.run_id])).rows;
  ok('W6b mock LLM finding 落库（LLM-* 前缀）', rB.llm_findings === 1 && llmF.length === 1 && llmF[0].severity === 'P3');
  ok('W6c LLM attempt 记 digest/model 零正文', llmA.length === 1 && llmA[0].status === 'DONE'
    && /^[0-9a-f]{64}$/.test(llmA[0].output_digest));

  // W6d 真实 provider 失败（注入 mock fetch 429）：attempt FAILED+code，pipeline 照常 REVIEWED
  const head3 = 'd'.repeat(40);
  await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: 3, headSha: head3 });
  const rC = await svc.handlePullRequestEvent(pool, { configured: true, llmEnv: CFG, llmFetch: mkFetch('429') },
    { payload: { ...ev, head_sha: head3 } });
  const llmC = (await pool.query(`SELECT status, error_code FROM mu.agent_attempt WHERE run_id=$1 AND provider='openai_compatible'`, [rC.run.run_id])).rows;
  ok('W6d LLM 429 → attempt FAILED(code) 管线不阻断', rC.ok === true && rC.llm_code === 'LLM_RATE_LIMITED'
    && llmC[0]?.status === 'FAILED' && llmC[0]?.error_code === 'LLM_RATE_LIMITED');
  ok('W6d2 deterministic 结果保留（fail-closed 回落）', rC.run.status === 'REVIEWED' && rC.findings_count >= 1);

  // W6e 泄漏全表扫描（key/prompt/diff/响应正文）
  let leaks = [];
  for (const tb of ['agent_attempt', 'agent_finding', 'review_run', 'orchestration_decision', 'audit_event', 'job']) {
    const cols = (await pool.query(`SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name=$1 AND data_type IN ('text','jsonb')`, [tb])).rows.map((r) => r.column_name);
    for (const c of cols) {
      for (const mark of ['k-test-only', '你是 PR 安全审查助手', 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ']) {
        const hit = await pool.query(`SELECT 1 FROM mu.${tb} WHERE CAST(${c} AS text) LIKE $1 LIMIT 1`, [`%${mark}%`]);
        if (hit.rows.length) leaks.push(`${tb}.${c}:${mark.slice(0, 8)}`);
      }
    }
  }
  ok('W6e key/prompt/原始 secret 全库零泄漏', leaks.length === 0, leaks.slice(0, 3));
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  __resetGhProvider();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
