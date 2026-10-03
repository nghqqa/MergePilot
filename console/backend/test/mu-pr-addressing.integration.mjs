#!/usr/bin/env node
// console/backend/test/mu-pr-addressing.integration.mjs — PR 详情 repo_id 寻址 + 审批票按 PR
// 过滤集成测试（审计 E-1/E-8 回归锁）。
//  * E-1：GET /api/mu/prs/:n 编号寻址必须消费 ?repo_id=（tenant 收窄内先定 repo 再定 PR）——
//    同租户双仓同号 PR 不串详情；repo 越界/未知 repo/未知 PR 同形 404（不泄露存在性）；
//    不带 repo_id 保持旧行为（webhook/内部调用向后兼容）；
//  * E-8：/prs/:n/fix-approvals 按 tenant+repo+pr 三维过滤——同仓不同 PR 票据互不可见；
//  * 权限/会话语义不变（401/403 RBAC/CSRF）；initSchema 重放幂等。
// 运行：node console/backend/test/mu-pr-addressing.integration.mjs
// （env: FXV_PG_TEST_DSN 已设则直连该库【共享库：全部标识符带随机后缀，可重复跑】；
//   未设则自起一次性 postgres:16-alpine 容器，跑毕 docker rm -f——不触常驻栈。）
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const { createConsole } = await import('../server.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

// ── 一次性 PG：优先共享 DSN（CI/任务书供给），否则自起容器 ──
let dsn = process.env.FXV_PG_TEST_DSN ?? null, container = null;
if (!dsn) {
  container = `mu-pra-${crypto.randomBytes(4).toString('hex')}`;
  const PGPORT = 16700 + Math.floor(Math.random() * 80);
  execFileSync('docker', ['run', '-d', '--name', container,
    '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
}
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'mu-pra-session-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_FIXTURES = '1';
process.env.MU_EXECUTOR = 'internal';
process.env.MU_EXECUTOR_INTERNAL_ALLOW = 'test';

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function muLogin(subject, tenantSlug) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject, ...(tenantSlug ? { tenant_slug: tenantSlug } : {}) }),
  });
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, cookie, csrf: json?.csrf ?? null, json };
}
async function call(p, { method = 'GET', body = null, cookie = null, csrf = null } = {}) {
  const res = await fetch(BASE + p, {
    method,
    headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
      ...(body ? { 'content-type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}

// ── 审批票真实路径造数：run→REVIEWED→P0 finding→Leader 消费（WAITING+PENDING 票）──
const orch = await import('../lib/multiuser/orchestration.mjs');
const ext = await import('../lib/multiuser/agents/external-reviewer.mjs');
async function mkPendingTicket(tenantId, repoId, pr, headSha) {
  const { run } = await orch.createRunIfAbsent(pool, { tenantId, repoId, prId: pr.pr_id, headSha });
  for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED']]) {
    await orch.transitionRun(pool, { runId: run.run_id, from: [f], to: t });
  }
  const binding = { tenantId, repoId, prId: pr.pr_id, headSha };
  const att = await orch.claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
    provider: 'deterministic', maxAttempts: 3, ...binding });
  await orch.insertFindings(pool, { attemptId: att.attemptId, runId: run.run_id, ...binding,
    findings: [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 'a.js', line_start: 3,
      title: 'hardcoded secret', summary_masked: 'sk-***' }] });
  await orch.finishAttempt(pool, { attemptId: att.attemptId, status: 'DONE' });
  const lead = await ext.leaderConsumeFindings(pool, { run, binding, protection: { configured: true } });
  if (lead?.decision !== 'fix_required') throw new Error('ticket setup failed: ' + JSON.stringify(lead).slice(0, 120));
  return run;
}

// 共享库可重复跑：全部标识符带随机后缀
const R = crypto.randomBytes(4).toString('hex');

try {
  // ══ 布置：tenant A（default）双仓 + tenant B 同名仓 + 同号 PR 矩阵 ══
  let admin = await muLogin('fixture:dev-pilot');
  if (admin.status !== 200) throw new Error('pilot login failed: ' + JSON.stringify(admin.json));
  const tenA = admin.json.tenant.tenant_id;

  for (const [login, role] of [[`pa-mnt-${R}`, 'maintainer'], [`pa-con-${R}`, 'contributor'], [`pa-aud-${R}`, 'auditor']]) {
    const r = await call('/api/mu/members', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
      body: { login, role } });
    if (r.status !== 200) throw new Error(`member ${login} setup failed: ` + JSON.stringify(r.json));
  }
  const mntA = await muLogin(`fixture:pa-mnt-${R}`, admin.json.tenant.slug);
  const conA = await muLogin(`fixture:pa-con-${R}`, admin.json.tenant.slug);
  const audA = await muLogin(`fixture:pa-aud-${R}`, admin.json.tenant.slug);

  const bindRepo = async (providerRepoId, owner, name) => {
    const r = await call('/api/mu/repositories', { method: 'POST', cookie: mntA.cookie, csrf: mntA.csrf,
      body: { provider_repo_id: providerRepoId, owner, name } });
    if (r.status !== 200) throw new Error(`bind ${owner}/${name} failed: ` + JSON.stringify(r.json));
    return r.json.repository.repo_id;
  };
  const repoA1 = await bindRepo(`PRA_${R}`, 'acme', 'alpha'); // tenant A 仓 1
  const repoA2 = await bindRepo(`PRB_${R}`, 'acme', 'beta');  // tenant A 仓 2

  // tenant B：同名仓库（同 provider_repo_id/owner/name，不同 tenant）
  const mkB = await call('/api/mu/tenants', { method: 'POST', cookie: admin.cookie, csrf: admin.csrf,
    body: { slug: `pa-b-${R}`, display_name: 'PR Addressing B' } });
  if (mkB.status !== 200) throw new Error('tenant B setup failed: ' + JSON.stringify(mkB.json));
  const tenB = mkB.json.tenant.tenant_id;
  const swB = await fetch(BASE + '/api/mu/auth/tenant', { method: 'POST',
    headers: { cookie: admin.cookie, 'content-type': 'application/json', 'x-csrf-token': admin.csrf },
    body: JSON.stringify({ tenant_id: tenB }) });
  const adminBCookie = (swB.headers.get('set-cookie') || '').split(';')[0];
  const adminBCsrf = (await swB.json())?.csrf;
  const addB = await call('/api/mu/members', { method: 'POST', cookie: adminBCookie, csrf: adminBCsrf,
    body: { login: `pb-mnt-${R}`, role: 'maintainer' } });
  if (addB.status !== 200) throw new Error('B member setup failed: ' + JSON.stringify(addB.json));
  const mntB = await muLogin(`fixture:pb-mnt-${R}`, `pa-b-${R}`);
  const bindRepoB = await call('/api/mu/repositories', { method: 'POST', cookie: mntB.cookie, csrf: mntB.csrf,
    body: { provider_repo_id: `PRA_${R}`, owner: 'acme', name: 'alpha' } }); // 与 repoA1 完全同名
  if (bindRepoB.status !== 200) throw new Error('B repo setup failed: ' + JSON.stringify(bindRepoB.json));
  const repoB1 = bindRepoB.json.repository.repo_id;

  // 播种：A2#7 先、A1#7 后（无 repo_id 的 legacy 编号寻址取最近更新行=A1）；
  // A1#8（审批票载体）；B1#7。head_sha 各不相同以便逐字段断言不串。
  const H2 = ('22' + R).padEnd(40, '0').slice(0, 40);
  const H1 = ('11' + R).padEnd(40, '0').slice(0, 40);
  const H8 = ('88' + R).padEnd(40, '0').slice(0, 40);
  const HB = ('bb' + R).padEnd(40, '0').slice(0, 40);
  const seed = async (repoId, number, head, title) => {
    const r = await call('/api/mu/fixtures/pr', { method: 'POST', cookie: mntA.cookie, csrf: mntA.csrf,
      body: { repo_id: repoId, number, head_sha: head, title } });
    if (r.status !== 200) throw new Error(`seed #${number} failed: ` + JSON.stringify(r.json));
    return r.json.pull_request;
  };
  const prA2n7 = await seed(repoA2, 7, H2, 'A2 #7');
  const prA1n7 = await seed(repoA1, 7, H1, 'A1 #7');
  const prA1n8 = await seed(repoA1, 8, H8, 'A1 #8');
  // 确定性：显式令 A1#7 为最近更新（防共享库时钟粒度巧合）
  await pool.query(`UPDATE mu.pull_request SET updated_at = now() WHERE pr_id=$1 AND tenant_id=$2`, [prA1n7.pr_id, tenA]);
  const seedB = await call('/api/mu/fixtures/pr', { method: 'POST', cookie: mntB.cookie, csrf: mntB.csrf,
    body: { repo_id: repoB1, number: 7, head_sha: HB, title: 'B1 #7' } });
  if (seedB.status !== 200) throw new Error('seed B #7 failed: ' + JSON.stringify(seedB.json));
  const prB1n7 = seedB.json.pull_request;

  // 审批票：A1#8 与 A2#7 各一张 P0 PENDING；A1#7 与 B1#7 无票
  await mkPendingTicket(tenA, repoA1, prA1n8, H8);
  await mkPendingTicket(tenA, repoA2, prA2n7, H2);
  const ticketsOf = async (prId) => (await pool.query(
    `SELECT approval_id, status FROM mu.fix_approval WHERE pr_id=$1 ORDER BY created_at`, [prId])).rows;

  // ══ PA*：E-1 编号寻址消费 repo_id ══
  const d1 = await call(`/api/mu/prs/7?repo_id=${repoA1}`, { cookie: conA.cookie });
  ok('PA1 双仓同号：?repo_id=A1 → 精确解析 A1#7（head/title 逐字段不串）',
    d1.status === 200 && d1.json?.pull_request?.repo_id === repoA1
      && d1.json.pull_request.head_sha === H1 && d1.json.pull_request.title === 'A1 #7'
      && d1.json.pull_request.pr_id === prA1n7.pr_id, d1.json?.pull_request);
  const d2 = await call(`/api/mu/prs/7?repo_id=${repoA2}`, { cookie: conA.cookie });
  ok('PA2 双仓同号：?repo_id=A2 → 精确解析 A2#7（基线缺陷：返回最近更新的 A1 行）',
    d2.status === 200 && d2.json?.pull_request?.repo_id === repoA2
      && d2.json.pull_request.head_sha === H2 && d2.json.pull_request.pr_id === prA2n7.pr_id,
    d2.json?.pull_request);

  const dLegacy = await call('/api/mu/prs/7', { cookie: conA.cookie });
  ok('PA3 不带 repo_id 保持旧行为（tenant 内最近更新行=A1#7；webhook/内部调用向后兼容）',
    dLegacy.status === 200 && dLegacy.json?.pull_request?.pr_id === prA1n7.pr_id, dLegacy.json?.pull_request);

  const dUuid = await call(`/api/mu/prs/${prA2n7.pr_id}?repo_id=${repoA2}`, { cookie: conA.cookie });
  ok('PA3b UUID+repo_id 配对寻址照常（repo 收窄不影响 UUID 寻址）',
    dUuid.status === 200 && dUuid.json?.pull_request?.pr_id === prA2n7.pr_id, dUuid.json?.pull_request);
  const dUuidX = await call(`/api/mu/prs/${prA2n7.pr_id}?repo_id=${repoA1}`, { cookie: conA.cookie });
  ok('PA3c UUID 与 repo_id 交叉配对 → 404（repo 收窄双保险）', dUuidX.status === 404, dUuidX.json);

  const missUnknownRepo = await call(`/api/mu/prs/7?repo_id=${crypto.randomUUID()}`, { cookie: conA.cookie });
  const missUnknownPr = await call('/api/mu/prs/999999?repo_id=' + repoA1, { cookie: conA.cookie });
  ok('PA4 repo 越界/未知 repo/未知 PR 三态同形 404（逐字节一致，不暴露存在性）',
    missUnknownRepo.status === 404 && missUnknownPr.status === 404
      && JSON.stringify(missUnknownRepo.json) === JSON.stringify(missUnknownPr.json)
      && missUnknownRepo.json?.error?.reason === 'pull_request_not_found',
    { repo: missUnknownRepo.json, pr: missUnknownPr.json });

  const dNumApprA1 = await call(`/api/mu/prs/7/fix-approvals?repo_id=${repoA1}`, { cookie: mntA.cookie });
  const dNumApprA2 = await call(`/api/mu/prs/7/fix-approvals?repo_id=${repoA2}`, { cookie: mntA.cookie });
  ok('PA5 编号寻址审批面板按 repo 收窄：A1#7 零票 / A2#7 恰一张 P0 PENDING',
    dNumApprA1.status === 200 && (dNumApprA1.json?.fix_approvals ?? []).length === 0
      && dNumApprA2.status === 200 && (dNumApprA2.json?.fix_approvals ?? []).length === 1
      && dNumApprA2.json.fix_approvals[0]?.severity === 'P0'
      && dNumApprA2.json.fix_approvals[0]?.status === 'PENDING'
      && String(dNumApprA2.json.fix_approvals[0]?.pr_number) === '7',
    { a1: dNumApprA1.json?.fix_approvals?.length, a2: dNumApprA2.json?.fix_approvals });

  // ══ PA*：E-8 同仓不同 PR 票据互不可见 ══
  const dAppr8 = await call(`/api/mu/prs/${prA1n8.pr_id}/fix-approvals`, { cookie: mntA.cookie });
  const dAppr7 = await call(`/api/mu/prs/${prA1n7.pr_id}/fix-approvals`, { cookie: mntA.cookie });
  ok('PA6 同仓不同 PR：A1#8 恰一张本 PR 票（pr_number=8）；A1#7 零票（不串 #8 的票）',
    dAppr8.status === 200 && (dAppr8.json?.fix_approvals ?? []).length === 1
      && String(dAppr8.json.fix_approvals[0]?.pr_number) === '8'
      && dAppr7.status === 200 && (dAppr7.json?.fix_approvals ?? []).length === 0,
    { n8: dAppr8.json?.fix_approvals?.length, n7: dAppr7.json?.fix_approvals?.length });

  // ══ PA*：跨租户同名仓库互不可见 ══
  const xBtoA = await call(`/api/mu/prs/7?repo_id=${repoA1}`, { cookie: mntB.cookie });
  const xAtoB = await call(`/api/mu/prs/7?repo_id=${repoB1}`, { cookie: conA.cookie });
  ok('PA7 两租户同名仓库互不可见：B 会话查 A 仓 / A 会话查 B 仓 → 同形 404',
    xBtoA.status === 404 && xAtoB.status === 404
      && JSON.stringify(xBtoA.json) === JSON.stringify(xAtoB.json)
      && JSON.stringify(xBtoA.json) === JSON.stringify(missUnknownPr.json),
    { b2a: xBtoA.json, a2b: xAtoB.json });
  const dB = await call('/api/mu/prs/7', { cookie: mntB.cookie });
  ok('PA7b tenant B 编号寻址只解析本租户行（B1#7，head=HB）',
    dB.status === 200 && dB.json?.pull_request?.pr_id === prB1n7.pr_id
      && dB.json.pull_request.head_sha === HB, dB.json?.pull_request);

  // ══ PA*：权限与会话语义不变 ══
  const unauth = await call(`/api/mu/prs/7?repo_id=${repoA1}`);
  ok('PA8 未登录 → 401（寻址参数不改变认证门）', unauth.status === 401);
  const audRead = await call(`/api/mu/prs/7?repo_id=${repoA1}`, { cookie: audA.cookie });
  const conRead = await call(`/api/mu/prs/7?repo_id=${repoA1}`, { cookie: conA.cookie });
  ok('PA9 RBAC 读矩阵不变：auditor 403 / contributor 200',
    audRead.status === 403 && conRead.status === 200, { aud: audRead.status, con: conRead.status });
  const noCsrf = await call(`/api/mu/prs/${prA1n7.pr_id}/decision`, { method: 'POST',
    cookie: mntA.cookie, body: { action: 'reject' } });
  ok('PA10 写操作缺 CSRF → 403 csrf_required（语义不变）',
    noCsrf.status === 403 && noCsrf.json?.error?.reason === 'csrf_required');

  // ══ PA*：审批决定边界（决定仍走 /approvals/:id，租户收窄不变）══
  const t8 = (await ticketsOf(prA1n8.pr_id))[0];
  const tA2 = (await ticketsOf(prA2n7.pr_id))[0];
  const decB = await call(`/api/mu/approvals/${tA2.approval_id}/reject`, { method: 'POST',
    cookie: mntB.cookie, csrf: mntB.csrf, body: { reason: 'cross-tenant probe' } });
  ok('PA11 跨租户审批决定 → 404 approval_not_found（B 维护者不可触碰 A 的票）',
    decB.status === 404 && decB.json?.error?.reason === 'approval_not_found', decB.json);
  const decA = await call(`/api/mu/approvals/${tA2.approval_id}/reject`, { method: 'POST',
    cookie: mntA.cookie, csrf: mntA.csrf, body: { reason: 'own-pr decision' } });
  ok('PA12 同租户维护者对本 PR 票决定照常（reject → REJECTED，走真实门）',
    decA.status === 200 && decA.json?.ticket?.status === 'REJECTED', decA.json);
  ok('PA13 被拒票落库（A2#7 REJECTED；A1#8 仍 PENDING——互不影响）',
    (await ticketsOf(prA2n7.pr_id))[0].status === 'REJECTED'
      && (await ticketsOf(prA1n8.pr_id))[0].status === 'PENDING');

  // ══ PA*：initSchema 重放幂等（重启语义）══
  const replayStore = await createMuStore({ pool });
  const r1 = await replayStore.initSchema();
  const r2 = await replayStore.initSchema();
  const d1r = await call(`/api/mu/prs/7?repo_id=${repoA1}`, { cookie: conA.cookie });
  const d2r = await call(`/api/mu/prs/7?repo_id=${repoA2}`, { cookie: conA.cookie });
  const appr8r = await call(`/api/mu/prs/${prA1n8.pr_id}/fix-approvals`, { cookie: mntA.cookie });
  ok('PA14 initSchema 重放两次成功且数据零损（PR 寻址/审批票计数与重放前一致）',
    r1 === true && r2 === true
      && d1r.json?.pull_request?.pr_id === prA1n7.pr_id && d2r.json?.pull_request?.pr_id === prA2n7.pr_id
      && (appr8r.json?.fix_approvals ?? []).length === 1,
    { r1, r2, d1: d1r.json?.pull_request?.pr_id, d2: d2r.json?.pull_request?.pr_id });
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
  Object.assign(process.env, savedEnv);
  server.close();
  await pool.end().catch(() => {});
  if (container) { try { execFileSync('docker', ['rm', '-f', '-v', container], { stdio: 'pipe' }); } catch { /* */ } }
}
console.log(`\nmu-pr-addressing.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
