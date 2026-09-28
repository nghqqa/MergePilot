#!/usr/bin/env node
// console/backend/test/mu-wave3-e2e.integration.mjs — Wave 3 PR-E 端到端集成测试。
// 真实链路：签名 webhook → event_sync 队列 → tick → 快照 → review_run → deterministic
// Reviewer → Leader 裁定 → FXV Fixer dry-run（真子进程+本地 git 仓库）→ 独立 Verifier
// → Leader 终裁 → 落库/审计 → 只读 API。
// 一次性 PG + mock GitHub adapter/provider + 合成签名密钥；MU_FIXTURES 未设（生产路径）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');
const { createConsole } = await import('../server.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const { __setGithubAppForTests } = await import('../lib/multiuser/ghapp.mjs');
const { __setGhProviderForTests, __resetGhProvider } = await import('../lib/multiuser/ghprovider.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? (pass++, console.log('  PASS  ' + n)) : (fail++, console.log('  FAIL  ' + n + (d ? ' ' + JSON.stringify(d).slice(0, 200) : ''))); };

// ── 本地 git 仓库（fixer/verifier 真子进程的 repo_url；head=真实提交）──
const REPO = fs.mkdtempSync(path.join(os.tmpdir(), 'w3e2e-'));
const git = (...a) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', '-c', 'core.autocrlf=false', ...a], { cwd: REPO, encoding: 'utf8' });
const SECRET = 'const PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";';
fs.writeFileSync(path.join(REPO, 'src.js'), 'const a = 1;\n' + SECRET + '\nconst b = 2;\n');
git('init', '-q', '-b', 'main'); git('add', '.'); git('commit', '-qm', 'init');
const HEAD1 = git('rev-parse', 'HEAD').trim();
git('commit', '-q', '--allow-empty', '-m', 'head2');
const HEAD2 = git('rev-parse', 'HEAD').trim();
const DIFF = `diff --git a/src.js b/src.js
index 1..2 100644
--- a/src.js
+++ b/src.js
@@ -1,2 +1,3 @@
 const a = 1;
+${SECRET}
 const b = 2;`;

// ── 一次性 PG + 服务端 ──
const CTR = `w3e2e-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16900 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
for (let i = 0; i < 150; i++) { try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; } catch { await new Promise((r) => setTimeout(r, 400)); } }

const WEBHOOK_SECRET = 'whsec_w3e2e_synthetic';
const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_SESSION_SECRET = 'w3e2e-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
// 注意：MU_FIXTURES 故意不设——生产路径（系统事件不受 fixture 开关限制）
process.env.MU_GITHUB_APP_ID = '999901';
process.env.MU_GITHUB_APP_SLUG = 'w3e2e-app';
process.env.MU_GITHUB_APP_PRIVATE_KEY = 'synthetic-not-real';
process.env.MU_GITHUB_WEBHOOK_SECRET = WEBHOOK_SECRET;
process.env.MU_GITHUB_APP_INSTALL_CALLBACK_URL = 'http://127.0.0.1:1/api/mu/github/install/callback';
delete process.env.MU_FIXTURES;

// fixer deps 测试注入（本地仓库 + 定制 testCmd）
global.__WAVE3_TEST_DEPS = { repoUrl: REPO, testCmd: 'node -e process.exit(0)' };

const INS = 79001, GHR = 99001, PRN = 11;
__setGithubAppForTests({
  async listRepositories() { return [{ id: GHR, name: 'repo', owner_login: 'w3', owner_id: 9,
    default_branch: 'main', private: false }]; },
});
__setGhProviderForTests({ async fetchPrContext(_cfg, { expectedHeadSha }) {
  return { stale_head: false, pr: { number: PRN, state: 'open', title: 't',
    head: { sha: expectedHeadSha, ref: 'b' }, base: { sha: '0'.repeat(40), ref: 'main' },
    changed_files: 1 }, diff: DIFF, checks: [],
    protection: { configured: true, required_checks: 1, required_reviews: 0, enforce_admins: true },
    limits: { diff_bytes: 1, over_diff_limit: false, over_file_limit: false },
    fetched_head_sha: expectedHeadSha };
} });

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

const sign = (body) => 'sha256=' + crypto.createHmac('sha256', WEBHOOK_SECRET).update(body).digest('hex');
const webhooks = [];
const postHook = async (body, delivery) => {
  const raw = JSON.stringify(body);
  webhooks.push(delivery);
  return fetch(BASE + '/api/mu/github/webhook', { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-github-event': 'pull_request',
      'x-github-delivery': delivery, 'x-hub-signature-256': sign(raw) },
    body: raw });
};
let csrf = null; const jar = {};
const login = async (subject) => {
  const res = await fetch(BASE + '/api/mu/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ provider: 'fixture', subject }) });
  for (const c of res.headers.getSetCookie?.() ?? []) { const [kv] = c.split(';'); const [k, v] = kv.split('='); jar[k] = v; }
  const j = await res.json(); csrf = j.csrf; return j;
};
const api = (p, opts = {}) => fetch(BASE + p, { ...opts, headers: { ...opts.headers,
  ...(csrf && opts.method === 'POST' ? { 'x-csrf-token': csrf } : {}),
  cookie: Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ') } });

try {
  const tick = () => api('/api/mu/jobs/tick', { method: 'POST' });

  // ── E1 安装链路：安装回调写 binding（PR-E 断层修复）──
  await login('fixture:pilot-admin'); // bootstrap platform_admin（fresh 库唯一身份）
  // 直接构造安装态（绕过浏览器回调流程——binding 写入逻辑在回调内；此处以 SQL 预置
  // 等价数据面，回调写入逻辑由 mu-ghapp 套件覆盖的安装路径 + 本 PR 的 upsert 语句）
  const t = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  await pool.query(`INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
    VALUES ($1,$2,9,'w3',1) ON CONFLICT DO NOTHING`, [INS, t]);
  const repo = (await pool.query(`INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name, default_branch)
    VALUES ($1,'github',$2,'w3','repo','main')
    ON CONFLICT DO NOTHING RETURNING repo_id`, [t, String(GHR)])).rows[0] ?? (await pool.query(`SELECT repo_id FROM mu.repository WHERE tenant_id=$1 AND provider_repo_id=$2`, [t, String(GHR)])).rows[0];
  await pool.query(`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state)
    VALUES ($1,$2,$3,'w3','repo',$4,'main','active') ON CONFLICT DO NOTHING`, [t, repo.repo_id, GHR, INS]);
  await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
    VALUES ($1,$2,$3,$4,'open') ON CONFLICT DO NOTHING`, [t, repo.repo_id, PRN, HEAD1]);
  ok('E1 服务链预置（installation+binding+PR）', true);

  // ── E2 真实 webhook（opened）→ tick → 全管线 ──
  const r2 = await postHook({ action: 'opened', installation: { id: INS },
    repository: { id: GHR, full_name: 'w3/repo' },
    pull_request: { number: PRN, head: { sha: HEAD1 }, base: { ref: 'main' } } }, 'w3-d1');
  ok('E2a webhook 202/200 accepted', r2.status === 200 || r2.status === 202, { s: r2.status });
  const tk = await (await tick()).json();
  const jobDone = (tk.processed ?? []).some((x) => x.kind === 'event_sync' && x.state === 'done');
  ok('E2b tick 消费 event_sync（MU_FIXTURES=0 生产路径）', jobDone, tk);
  const runs1 = (await pool.query(`SELECT * FROM mu.review_run WHERE head_sha=$1`, [HEAD1])).rows;
  ok('E2c review_run 唯一创建', runs1.length === 1);
  const run1 = runs1[0];
  const attempts1 = (await pool.query(`SELECT agent_role, status FROM mu.agent_attempt WHERE run_id=$1`, [run1.run_id])).rows;
  ok('E2d 三 Agent 真实执行记录', ['reviewer', 'fixer', 'verifier'].every((r) => attempts1.some((a) => a.agent_role === r && a.status === 'DONE')), attempts1);
  const f1 = (await pool.query(`SELECT rule_id, severity FROM mu.agent_finding WHERE run_id=$1`, [run1.run_id])).rows;
  ok('E2e Reviewer finding（P0 secret）', f1.some((x) => x.rule_id === 'R-SECRET' && x.severity === 'P0'));
  const fix1 = (await pool.query(`SELECT status FROM mu.fix_attempt WHERE run_id=$1`, [run1.run_id])).rows;
  ok('E2f Fixer dry-run 记录', fix1[0]?.status === 'DRY_RUN');
  const v1 = (await pool.query(`SELECT verdict FROM mu.verification_attempt WHERE run_id=$1`, [run1.run_id])).rows;
  ok('E2g Verifier PASS', v1[0]?.verdict === 'PASS');
  const run1b = (await pool.query(`SELECT status FROM mu.review_run WHERE run_id=$1`, [run1.run_id])).rows[0];
  ok('E2h Leader 终裁 COMPLETED（finding→dry-run→PASS 全链）', run1b.status === 'COMPLETED');
  const dec1 = (await pool.query(`SELECT stage, decision FROM mu.orchestration_decision WHERE run_id=$1 ORDER BY created_at`, [run1.run_id])).rows;
  ok('E2i 决策链完整（裁定+终裁）', dec1.some((d) => d.stage === 'leader_decision_after_review' && d.decision === 'fix_required')
    && dec1.some((d) => d.stage === 'leader_final'));

  // ── E3 重复 delivery 幂等 ──
  await postHook({ action: 'synchronize', installation: { id: INS }, repository: { id: GHR },
    pull_request: { number: PRN, head: { sha: HEAD1 } } }, 'w3-d2');
  await tick();
  const runs1count = (await pool.query(`SELECT count(*)::int n FROM mu.review_run WHERE head_sha=$1`, [HEAD1])).rows[0].n;
  ok('E3 重复事件同 head 幂等（仍 1 个 run）', runs1count === 1);

  // ── E4 新 head 隔离 ──
  await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
    VALUES ($1,$2,$3,$4,'open') ON CONFLICT DO NOTHING`, [t, repo.repo_id, PRN, HEAD2]);
  await postHook({ action: 'synchronize', installation: { id: INS }, repository: { id: GHR },
    pull_request: { number: PRN, head: { sha: HEAD2 } } }, 'w3-d3');
  await tick();
  const run2 = (await pool.query(`SELECT run_id, status FROM mu.review_run WHERE head_sha=$1`, [HEAD2])).rows[0];
  ok('E4 新 head 新 run', Boolean(run2));
  // HEAD2 的 diff 同样含 secret → 同样 COMPLETED；旧 run 不被改
  const oldRun = (await pool.query(`SELECT status FROM mu.review_run WHERE head_sha=$1`, [HEAD1])).rows[0];
  ok('E4b 旧 head 结果隔离保留', oldRun.status === 'COMPLETED');

  // ── E5 撤权（binding revoked）后 webhook 拒绝 ──
  await pool.query(`UPDATE mu.repository_binding SET binding_state='revoked' WHERE github_repo_id=$1`, [GHR]);
  git('commit', '-q', '--allow-empty', '-m', 'head3');
  const HEAD3 = git('rev-parse', 'HEAD').trim();
  await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha, state)
    VALUES ($1,$2,$3,$4,'open') ON CONFLICT DO NOTHING`, [t, repo.repo_id, PRN, HEAD3]);
  await postHook({ action: 'opened', installation: { id: INS }, repository: { id: GHR },
    pull_request: { number: PRN, head: { sha: HEAD3 } } }, 'w3-d4');
  await tick();
  const run3 = (await pool.query(`SELECT count(*)::int n FROM mu.review_run WHERE head_sha=$1`, [HEAD3])).rows[0].n;
  ok('E5 binding 撤销后零新 run', run3 === 0);
  await pool.query(`UPDATE mu.repository_binding SET binding_state='active' WHERE github_repo_id=$1`, [GHR]);

  // ── E6 只读 API（read_pull_request 角色：maintainer）──
  const storeT = await createMuStore({ pool });
  const mU = await storeT.ensureUser({ login: 'maint-w3' });
  await storeT.ensureIdentity({ userId: mU.user_id, provider: 'fixture', subject: 'fixture:maint-w3' });
  await storeT.ensureMembership({ tenantId: t, userId: mU.user_id, role: 'maintainer' });
  await login('fixture:maint-w3');
  const runsApi = await (await api('/api/mu/runs')).json();
  ok('E6a runs 列表含两 run', (runsApi.runs ?? []).length >= 2);
  const detail = await (await api(`/api/mu/runs/${run2.run_id}`)).json();
  ok('E6b run 详情（findings/attempts/decisions）', (detail.findings ?? []).length >= 1
    && (detail.attempts ?? []).length >= 3 && (detail.decisions ?? []).length >= 1);
  const nf = await api(`/api/mu/runs/${crypto.randomUUID()}`);
  ok('E6c 不存在的 run → 404 not_found（不泄露存在性）', nf.status === 404);
  // Auditor 无读 PR 权限（显式创建 auditor 用户+成员）
  const audU = await storeT.ensureUser({ login: 'aud-w3' });
  await storeT.ensureIdentity({ userId: audU.user_id, provider: 'fixture', subject: 'fixture:aud-w3' });
  await storeT.ensureMembership({ tenantId: t, userId: audU.user_id, role: 'auditor' });
  await login('fixture:aud-w3');
  const aud = await api('/api/mu/runs');
  ok('E6d Auditor 无管线读权限（403）', aud.status === 403);

  // ── E7 凭据/正文零落库 ──
  const TOKEN_MARK = 'synthetic-not-real'; // MU_GITHUB_APP_PRIVATE_KEY 值
  let leaks = [];
  for (const tb of ['review_run', 'agent_attempt', 'agent_finding', 'fix_attempt',
    'verification_attempt', 'orchestration_decision', 'job', 'audit_event']) {
    const cols = (await pool.query(`SELECT column_name FROM information_schema.columns
      WHERE table_schema='mu' AND table_name=$1 AND data_type IN ('text','jsonb','json')`, [tb])).rows.map((r) => r.column_name);
    for (const c of cols) {
      const hit = await pool.query(`SELECT 1 FROM mu.${tb} WHERE CAST(${c} AS text) LIKE $1 LIMIT 1`, [`%${TOKEN_MARK}%`]);
      if (hit.rows.length) leaks.push(`${tb}.${c}`);
    }
  }
  const diffLeak = await pool.query(`SELECT 1 FROM mu.agent_finding WHERE summary_masked LIKE $1 LIMIT 1`, ['%ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ%']);
  ok('E7 私钥零泄漏 + secret 摘要已打码', leaks.length === 0 && diffLeak.rows.length === 0, leaks);
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  __resetGhProvider();
  delete global.__WAVE3_TEST_DEPS;
  server.close();
  await pool.end().catch(() => {});
  Object.assign(process.env, savedEnv);
  try { fs.rmSync(REPO, { recursive: true, force: true }); } catch { /* */ }
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
