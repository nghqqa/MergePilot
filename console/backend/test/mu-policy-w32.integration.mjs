#!/usr/bin/env node
// console/backend/test/mu-policy-w32.integration.mjs — Wave 3.2 策略控制面集成测试。
// 一次性 PG + createConsole 真服务；零真实模型调用（env 缺失/mock 路径）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');
const { createConsole } = await import('../server.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const apMod = await import('../lib/multiuser/agent-policy.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const svc = await import('../lib/multiuser/review-service.mjs');
const { __setGhProviderForTests, __resetGhProvider } = await import('../lib/multiuser/ghprovider.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { if (c) { pass++; console.log('  PASS  ' + n); } else { fail++; console.log('  FAIL  ' + n + (d ? ' ' + JSON.stringify(d).slice(0, 160) : '')); } };

const CTR = `w32-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17200 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 150; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 400)); } }

const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_SESSION_SECRET = 'w32-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_BOOTSTRAP_ADMIN_LOGIN = 'dev-pilot';
delete process.env.MU_LLM_PROVIDER; // 部署 env 缺失（fail-closed 场景基线）

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
let csrf = null; const jar = {};
const login = async (subject) => {
  const res = await fetch(BASE + '/api/mu/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ provider: 'fixture', subject }) });
  for (const c of res.headers.getSetCookie?.() ?? []) { const [kv] = c.split(';'); const [k, v] = kv.split('='); jar[k] = v; }
  csrf = (await res.json()).csrf;
};
const api = (p, opts = {}) => fetch(BASE + p, { ...opts, headers: { ...opts.headers,
  ...(csrf && opts.method !== 'GET' ? { 'x-csrf-token': csrf } : {}),
  cookie: Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ') } });

try {
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();

  // ── W1 迁移与默认 ──
  const mig = await pool.query(`SELECT 1 FROM mu.schema_migrations WHERE version=11`);
  ok('W1a migration v11 应用', mig.rows.length === 1);
  const def = await apMod.getAgentPolicy(pool);
  ok('W1b 默认 deterministic_only + disabled', def.mode === 'deterministic_only' && def.enabled === false && Number(def.policy_version) === 1);

  // 重复 initSchema 幂等 + 回滚重放
  await store.initSchema();
  const rows2 = await pool.query(`SELECT count(*)::int n FROM mu.agent_policy`);
  ok('W1c 重复迁移零重复行', rows2.rows[0].n === 1);
  await pool.query(`DELETE FROM mu.schema_migrations WHERE version=11`);
  await pool.query(`DROP TABLE mu.agent_policy`);
  await pool.query(`ALTER TABLE mu.review_run DROP COLUMN IF EXISTS agent_policy_version`);
  await pool.query(`ALTER TABLE mu.review_run DROP COLUMN IF EXISTS llm_mode`);
  await store.initSchema(); // 回滚后重放
  const def2 = await apMod.getAgentPolicy(pool);
  ok('W1d 回滚→重放自愈（默认行重建）', def2.mode === 'deterministic_only' && Number(def2.policy_version) === 1);

  // ── W2 API：PlatformAdmin 读写 ──
  await login('fixture:dev-pilot'); // bootstrap platform_admin
  const g1r = await api('/api/mu/agent-policy');
  const g1 = await g1r.json().catch(() => null);
  ok('W2a GET 返回策略+脱敏 deploy 状态', g1.policy.mode === 'deterministic_only'
    && g1.deploy.provider_configured === false && Array.isArray(g1.deploy.allowed_models)
    && g1.runtime_state === 'env_not_configured'
    && !JSON.stringify(g1).includes('MU_LLM_API_KEY'));
  // 确认门
  const noConfirm = await api('/api/mu/agent-policy', { method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ expected_version: 1, mode: 'llm_assist', enabled: true }) });
  ok('W2b 无 confirm 拒绝', noConfirm.status === 400 && (await noConfirm.json()).error.reason === 'confirm_required');
  // 成功更新（开启 llm_assist）
  const put1 = await api('/api/mu/agent-policy', { method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ expected_version: 1, mode: 'llm_assist', enabled: true,
      model: 'deepseek-flash', timeout_ms: 45000, max_output_tokens: 2048, confirm: true }) });
  const put1j = await put1.json();
  ok('W2c PUT 成功且版本 +1', put1.status === 200 && put1j.policy.policy_version === 2 && put1j.policy.mode === 'llm_assist');
  // runtime_state：env 缺失 → policy_enabled_but_env_invalid
  const g2 = await (await api('/api/mu/agent-policy')).json();
  ok('W2d env 缺失 + 策略启用 → fail-closed 状态', g2.runtime_state === 'policy_enabled_but_env_invalid');

  // ── W3 CAS 版本冲突 ──
  const putStale = await api('/api/mu/agent-policy', { method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ expected_version: 1, enabled: false, confirm: true }) });
  ok('W3 旧版本 CAS 冲突 → 409 + 当前版本', putStale.status === 409
    && (await putStale.json()).error.current_policy_version === 2);

  // ── W4 非法值拒绝（服务端白名单 + DB CHECK）──
  for (const [patch, why] of [
    [{ model: 'gpt-4o' }, '模型不在白名单'],
    [{ model: 'Bad Model!' }, '模型形状非法'],
    [{ timeout_ms: 10 }, '超时下越界'],
    [{ timeout_ms: 999999 }, '超时上越界'],
    [{ max_output_tokens: 1 }, 'tokens 下越界'],
    [{ provider: 'custom' }, 'provider 非白名单'],
    [{ mode: 'agentic' }, 'mode 非法'],
    [{ api_key: 'sk-xxx' }, '凭据字段'],
    [{ base_url: 'https://evil' }, 'endpoint 字段'],
  ]) {
    const r = await api('/api/mu/agent-policy', { method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ expected_version: 2, confirm: true, ...patch }) });
    ok(`W4 ${why} 拒绝（${(await r.json().catch(() => ({})))?.error?.reason}）`, r.status === 400);
  }

  // ── W5 五角色越权 ──
  const T = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  const roles = [['maintainer', 'maint'], ['reviewer', 'rev'], ['contributor', 'con'], ['auditor', 'aud']];
  for (const [role, slug] of roles) {
    const u = await store.ensureUser({ login: `w32-${slug}` });
    await store.ensureIdentity({ userId: u.user_id, provider: 'fixture', subject: `fixture:w32-${slug}` });
    await store.ensureMembership({ tenantId: T, userId: u.user_id, role });
    await login(`fixture:w32-${slug}`);
    const rGet = await api('/api/mu/agent-policy');
    const rPut = await api('/api/mu/agent-policy', { method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ expected_version: 2, enabled: false, confirm: true }) });
    ok(`W5 ${role} GET/PUT 均 403`, rGet.status === 403 && rPut.status === 403);
  }

  // ── W6 CSRF ──
  await login('fixture:dev-pilot');
  const noCsrf = await fetch(BASE + '/api/mu/agent-policy', { method: 'PUT',
    headers: { 'content-type': 'application/json', cookie: Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ') },
    body: JSON.stringify({ expected_version: 2, enabled: false, confirm: true }) });
  ok('W6 缺 CSRF → 403', noCsrf.status === 403);

  // ── W7 env 缺失 fail-closed：llm_assist 启用但回落 deterministic ──
  const repo = await store.ensureRepository({ tenantId: T, provider: 'github',
    providerRepoId: '99601', owner: 'w32', name: 'r', defaultBranch: 'main' });
  await pool.query(`INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id) VALUES (1,$1,1,'w32',1) ON CONFLICT DO NOTHING`, [T]);
  await pool.query(`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state) VALUES ($1,$2,99601,'w32','r',1,'main','active') ON CONFLICT DO NOTHING`, [T, repo.repo_id]);
  const HEAD1 = '1'.repeat(40), HEAD2 = '2'.repeat(40);
  for (const h of [HEAD1, HEAD2]) {
    await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: 7, headSha: h });
  }
  const CLEAN_DIFF = 'diff --git a/a.js b/a.js\nindex 1..2 100644\n--- a/a.js\n+++ b/a.js\n@@ -1,1 +1,2 @@\n const a = 1;\nconst b = 2;';
  __setGhProviderForTests({ async fetchPrContext(_c, { expectedHeadSha }) {
    return { stale_head: false, pr: { number: 7, state: 'open', title: 't',
      head: { sha: expectedHeadSha }, base: { ref: 'main' }, changed_files: 1 },
      diff: CLEAN_DIFF, checks: [], protection: { configured: true },
      limits: { diff_bytes: 1, over_diff_limit: false }, fetched_head_sha: expectedHeadSha };
  } });
  const r7 = await svc.handlePullRequestEvent(pool, { configured: true }, { payload: {
    action: 'opened', github_repo_id: 99601, pr_number: 7, head_sha: HEAD1 } });
  const llmAtt7 = (await pool.query(`SELECT count(*)::int n FROM mu.agent_attempt WHERE run_id=$1 AND provider IN ('deterministic_mock','openai_compatible')`, [r7.run.run_id])).rows[0].n;
  ok('W7 env 缺失：零 LLM attempt + 如实 reason + deterministic 完成',
    r7.ok === true && llmAtt7 === 0 && r7.llm_code === 'LLM_POLICY_ENV_MISMATCH' && r7.run.status === 'REVIEWED');
  ok('W7b run 快照列冻结（v2 + llm_assist）', Number(r7.run.agent_policy_version) === 2 && r7.run.llm_mode === 'llm_assist');

  // ── W8 快照冻结：更新策略后新 run 用新版本，旧 run 不变 ──
  const up = await apMod.updateAgentPolicy(pool, { expectedVersion: 2, patch: { enabled: false, mode: 'deterministic_only' }, actorId: null });
  ok('W8a 策略更新到 v3', up.ok === true && up.policy.policy_version === 3);
  const r8 = await svc.handlePullRequestEvent(pool, { configured: true }, { payload: {
    action: 'synchronize', github_repo_id: 99601, pr_number: 7, head_sha: HEAD2 } });
  ok('W8b 新 run 冻结新版本（v3 + deterministic_only）',
    Number(r8.run.agent_policy_version) === 3 && r8.run.llm_mode === 'deterministic_only');
  const oldRun = await orch.getRun(pool, r7.run.run_id);
  ok('W8c 旧 run 快照不被改写（仍 v2）', Number(oldRun.agent_policy_version) === 2 && oldRun.llm_mode === 'llm_assist');

  // ── W9 审计脱敏 ──
  const aud = await pool.query(`SELECT detail FROM mu.audit_event WHERE kind='AGENT_POLICY_UPDATED' ORDER BY seq DESC LIMIT 3`);
  const audStr = JSON.stringify(aud.rows);
  ok('W9 审计只含策略字段（无 env/凭据/URL）', aud.rows.length >= 1
    && !/api_key|sk-|MU_LLM_API_KEY|https?:\/\//.test(audStr));

  // ── W10 表内零凭据形状列 ──
  const cols = (await pool.query(`SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name='agent_policy'`)).rows.map((r) => r.column_name);
  ok('W10 agent_policy 零凭据形状列', !cols.some((c) => /api[_-]?key|secret|password|private|^token$/i.test(c)), cols);
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  __resetGhProvider();
  server.close();
  await pool.end().catch(() => {});
  Object.assign(process.env, savedEnv);
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
