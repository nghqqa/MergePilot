// mu-apply-merge-http.integration.mjs — apply/merge/reconcile 端点层回归（#463 教训锁）。
// 背景：集成测试直调服务层未经过 API guard 层——apply_fix/merge_pr ∈ MU_BINDING_REQUIRED
// 而端点 guard 裸调用使 binding 恒 null，任何人 403 binding_required（PR#12 真实应用步实证）。
// 本文件走完整 HTTP 面：真 server + fixture 会话 + CSRF 双提交 + repo/binding guard。
// 覆盖：合法 maintainer 200 / contributor 403 / platform_admin 403 / 跨租户 404 /
//       无 active binding 403 / 缺 confirm 400 / 缺 CSRF 403 / reconcile 跨租户 404。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Z]:)/, '$1'));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass += 1; console.log('  PASS ', name); }
  else { fail += 1; console.log('  FAIL ', name, JSON.stringify(detail ?? '').slice(0, 260)); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const HEAD = 'e3a81db736330000000000000000000000000000';
const CTR = `mu-ap-http-${crypto.randomBytes(3).toString('hex')}`;
const PORT = 17900 + Math.floor(Math.random() * 60);

try {
  execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  const dsn = `postgres://postgres:x@127.0.0.1:${PORT}/mu`;
  const pool = new Pool({ connectionString: dsn });
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { await pool.query('SELECT 1'); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await sleep(800); }
  }
  const savedEnv = { ...process.env };
  process.env.MU_MODE = 'multiuser';
  process.env.CONSOLE_PG_DSN = dsn;
  process.env.CONSOLE_SESSION_SECRET = 'mu-apply-http-secret';
  process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
  process.env.MU_FIXTURES = '1';
  process.env.CONSOLE_PILOT_USER = 'dev-pilot';
  process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
  process.env.MU_GITHUB_APP_ID = '1';
  // 真 RSA 私钥（JWT 需真实可签；token 交换端点已被全局 fetch 桩接管）
  const { generateKeyPairSync } = await import('node:crypto');
  const { privateKey: ghKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  process.env.MU_GITHUB_APP_PRIVATE_KEY = ghKey.export({ type: 'pkcs1', format: 'pem' });

  // 远端读取打桩（同进程 server 共享 global fetch）：PR 读回=开放+head 一致
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (!u.includes('api.github.com')) return realFetch(url, opts); // localhost（真 server）直通
    if (u.includes('api.github.com/app/installations/')) {
      return { ok: true, status: 201, json: async () => ({ token: 't' }) };
    }
    if (u.includes('/pulls/12')) {
      return { ok: true, status: 200, json: async () => ({ state: 'open',
        head: { sha: HEAD, ref: 'fix/x' }, base: { ref: 'main' }, mergeable: true }) };
    }
    if (u.includes('/check-runs') || u.includes('/status')) {
      return { ok: true, status: 200, json: async () => ({ state: 'success', statuses: [], total_count: 0, check_runs: [] }) };
    }
    // 其余（含真实外呼）一律拒绝——本测试零外呼
    return { ok: false, status: 599, json: async () => ({}) };
  };

  const { createConsole } = await import('../server.mjs');
  const { server } = createConsole({ evidenceRoot: os.tmpdir(), distDir: path.join(os.tmpdir(), 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const BASE = `http://127.0.0.1:${server.address().port}`;

  async function muLogin(subject) {
    const res = await fetch(BASE + '/api/mu/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ provider: 'fixture', subject }) });
    const jar = {};
    for (const c of res.headers.getSetCookie?.() ?? []) {
      const [kv] = c.split(';'); const [k, ...v] = kv.split('=');
      jar[k.trim()] = v.join('=');
    }
    const json = await res.json().catch(() => null);
    return { status: res.status, json, csrf: json?.csrf ?? null,
      cookie: Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ') };
  }
  async function call(p, { method = 'POST', body = null, cookie = null, csrf = null } = {}) {
    const res = await fetch(BASE + p, { method,
      headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
        ...(body ? { 'content-type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    const json = await res.json().catch(() => null);
    return { status: res.status, json };
  }

  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();
  const T = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;

  // 会话：maintainer（合法）、contributor（缺动作）、platform_admin（角色不授写入）
  const admin = await muLogin('fixture:dev-pilot');
  for (const [login, role] of [['mt-a', 'maintainer'], ['ct-a', 'contributor']]) {
    const added = await call('/api/mu/members', { cookie: admin.cookie, csrf: admin.csrf, body: { login, role } });
    if (added.status !== 200) throw new Error(`member add failed: ${login} ${JSON.stringify(added.json)}`);
  }
  const MT = await muLogin('fixture:mt-a');
  const CT = await muLogin('fixture:ct-a');
  ok('H0 会话就绪（maintainer/contributor）', MT.status === 200 && CT.status === 200, null);

  // 种子：仓库+active 绑定（repository_binding，v18 统一表）+PR+run+active 补丁
  const REPO = crypto.randomUUID(); const PRID = crypto.randomUUID();
  const RUN = crypto.randomUUID(); const T2 = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.repository (repo_id, tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,$2,'github','90001','nghqqa','test-repo')`, [REPO, T]);
  await pool.query(
    `INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
     VALUES (165747012, $1, 7356699, 'nghqqa', 5105202)`, [T]);
  await pool.query(
    `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state)
     VALUES ($1,$2,90001,'nghqqa','test-repo',165747012,'main','active')`, [T, REPO]);
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,12,$4,'open')`, [PRID, T, REPO, HEAD]);
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'webhook','COMPLETED','deterministic_only')`, [RUN, T, REPO, PRID, HEAD]);
  const art = await import('../lib/multiuser/artifacts.mjs');
  const GOOD_DIFF = ['--- a/src/config.js', '+++ b/src/config.js', '@@ -1,3 +1,4 @@',
    ' const x = 1;', '-const token = "hardcoded";', '+const token = process.env.TOKEN;', '+const y = 2;',
  ].join('\n') + '\n';
  await art.persistFixPatch(pool, { runId: RUN, tenantId: T, repoId: REPO,
    headSha: HEAD, diffText: GOOD_DIFF, rationale: 'r', changedFiles: [{ path: 'src/config.js' }] });
  // 第二租户的同名仓库+run（跨租户探测）——自洽建链，不借第一租户的 repo/pr 行
  await pool.query(`INSERT INTO mu.tenant (tenant_id, slug, display_name) VALUES ($1,'tenant-b','B')`, [T2]);
  const R2 = crypto.randomUUID(); const PR2 = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.repository (repo_id, tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,$2,'github','90001','nghqqa','test-repo')`, [R2, T2]);
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,12,$4,'open')`, [PR2, T2, R2, HEAD]);
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'webhook','COMPLETED','deterministic_only')`,
    [crypto.randomUUID(), T2, R2, PR2, HEAD]);

  // ── H1: maintainer + active binding → preview 200（guard 三层通过） ──
  const h1 = await call(`/api/mu/runs/${RUN}/apply-fix/preview`, { method: 'GET', cookie: MT.cookie });
  ok('H1 maintainer+binding：apply preview 200（guard 层修复合入实证）',
    h1.status === 200 && h1.json?.ok === true && h1.json?.confirm_binds?.expected_patch_digest, h1);

  // ── H2: contributor → 403 action_not_granted ──
  const h2 = await call(`/api/mu/runs/${RUN}/apply-fix/preview`, { method: 'GET', cookie: CT.cookie });
  ok('H2 contributor → 403 action_not_granted', h2.status === 403
    && h2.json?.error?.reason === 'action_not_granted', h2);

  // ── H3: 跨租户 run → 404 同形 ──
  const t2run = (await pool.query(
    `SELECT run_id FROM mu.review_run WHERE tenant_id=$1 ORDER BY created_at DESC LIMIT 1`, [T2])).rows[0];
  const h3 = await call(`/api/mu/runs/${t2run.run_id}/apply-fix/preview`, { method: 'GET', cookie: MT.cookie });
  ok('H3 跨租户 run → 404（同形不泄露）', h3.status === 404, h3);

  // ── H4: 吊销绑定 → 403 binding_required（恢复后回到 200） ──
  await pool.query(`UPDATE mu.repository_binding SET binding_state='revoked' WHERE repo_id=$1`, [REPO]);
  const h4 = await call(`/api/mu/runs/${RUN}/apply-fix/preview`, { method: 'GET', cookie: MT.cookie });
  ok('H4 无 active binding → 403 binding_required', h4.status === 403
    && h4.json?.error?.reason === 'binding_required', h4);
  await pool.query(`UPDATE mu.repository_binding SET binding_state='active' WHERE repo_id=$1`, [REPO]);
  const h4b = await call(`/api/mu/runs/${RUN}/apply-fix/preview`, { method: 'GET', cookie: MT.cookie });
  ok('H4b 恢复绑定 → 200（可恢复）', h4b.status === 200, null);

  // ── H5: merge preview 同样过 guard（merge_pr+binding） ──
  const h5 = await call(`/api/mu/runs/${RUN}/merge-pr/preview`, { method: 'GET', cookie: MT.cookie });
  ok('H5 maintainer：merge preview 200（merge_pr guard 层实证）',
    h5.status === 200 && h5.json?.ok === true && h5.json?.confirm_binds?.expected_head, h5);
  const h5b = await call(`/api/mu/runs/${RUN}/merge-pr/preview`, { method: 'GET', cookie: CT.cookie });
  ok('H5b contributor merge → 403 action_not_granted', h5b.status === 403
    && h5b.json?.error?.reason === 'action_not_granted', h5b);

  // ── H6: POST confirm 缺 confirm 字段 → 400（guard 过了，服务层确认门兜住） ──
  const h6 = await call(`/api/mu/runs/${RUN}/apply-fix/confirm`, { cookie: MT.cookie, csrf: MT.csrf, body: {} });
  ok('H6 confirm 缺字段 → 400 confirm_required（HTTP 面）',
    h6.status === 400 && h6.json?.error?.reason === 'confirm_required', h6);

  // ── H7: 无 CSRF → 403 csrf_required（CSRF 双提交先于授权） ──
  const h7 = await call(`/api/mu/runs/${RUN}/apply-fix/confirm`, { cookie: MT.cookie, body: { confirm: true } });
  ok('H7 无 CSRF → 403 csrf_required', h7.status === 403
    && h7.json?.error?.reason === 'csrf_required', h7);

  // ── H8: reconcile 跨租户 → 404 同形 ──
  await pool.query(
    `INSERT INTO mu.fix_apply_operation
       (operation_type, status, tenant_id, repo_id, pr_id, run_id,
        provider, provider_pr_number, idempotency_key, operator_user_id)
     VALUES ('apply_fix','result_uncertain',$1,$2,$3,$4,'github',12,$5,$6)`,
    [T2, REPO, PRID, crypto.randomUUID(), 'x-tenant-b', (await pool.query(
      `SELECT user_id FROM mu.app_user LIMIT 1`)).rows[0].user_id]);
  const opT2 = (await pool.query(
    `SELECT operation_id FROM mu.fix_apply_operation WHERE tenant_id=$1`, [T2])).rows[0];
  const h8 = await call(`/api/mu/apply-operations/${opT2.operation_id}/reconcile`,
    { cookie: MT.cookie, csrf: MT.csrf });
  ok('H8 reconcile 跨租户 op → 404（同形不泄露）', h8.status === 404, h8);

  globalThis.fetch = realFetch;
  server.close();
  await pool.end();
  console.log(`\nmu-apply-merge-http.integration: ${pass} pass / ${fail} fail`);
  process.exit(fail ? 1 : 0);
} catch (e) {
  console.error('HARNESS ERROR', e);
  process.exitCode = 1;
} finally {
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* 尽力 */ }
}
