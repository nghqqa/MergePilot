#!/usr/bin/env node
// console/backend/test/mu-usage.integration.mjs — rc.11 PR-B：最小用量统计面集成测试。
// 覆盖：/api/mu/usage/summary|by-skill|by-period 三只读聚合——summary 数值与手算一致
//（total/succeeded/failed/cancelled/timeout/success_rate/p50/p95/last_called_at）、
// by-skill 分组+排序+分页+总组数、by-period 按日分组（UTC，无数据日不补零）、
// 过滤参数（window/skill_key/agent_role/status/repo_id/pr_id）各自生效、非法参数 400、
// 跨租户 404/零泄漏、401/403 矩阵、token_metering/cost unavailable 块、
// digest 零正文（响应不含任何 digest 列）、v2 前"无留痕"对照（不伪装成零用量外的数据）。
// 自 boot postgres + fixture 登录 harness（参照 mu-facade-rbac.integration.mjs）。
// 运行：node console/backend/test/mu-usage.integration.mjs
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const here = path.dirname(fileURLToPath(import.meta.url));
const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 260) : ''}`); }
};
const sha256 = (v) => crypto.createHash('sha256').update(String(v)).digest('hex');

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = process.env.CONSOLE_PG_DSN || ''; // 由下方 docker 容器填充
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'usage-it-session-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
delete process.env.MU_FIXTURES;

const CTR = `mu-usage-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16500 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
process.env.CONSOLE_PG_DSN = dsn;
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

const { createConsole } = await import('../server.mjs');
const { server } = createConsole({ evidenceRoot: here, distDir: path.join(here, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function muLogin(subject) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject }),
  });
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, cookie, csrf: json?.csrf ?? null, json };
}
async function call(p, { method = 'GET', cookie = null } = {}) {
  const res = await fetch(BASE + p, { method, headers: cookie ? { cookie } : {} });
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, json };
}

// 事件种子（手算基准见各断言；idempotency_key 每租户唯一）
let idemN = 0;
async function seedSkill({ tenantId, repoId, prId, runId, role, key, status, latency, startedAt }) {
  idemN += 1;
  await pool.query(
    `INSERT INTO mu.skill_invocation_event
       (tenant_id, repo_id, pr_id, run_id, agent_role, skill_key, skill_version,
        invocation_kind, status, started_at, completed_at, latency_ms, idempotency_key)
     VALUES ($1,$2,$3,$4,$5,$6,'v1','verifier_tool',$7,$8::timestamptz,
             CASE WHEN $7='RUNNING' THEN NULL ELSE $8::timestamptz + make_interval(secs => 1) END,
             $9, $10)`,
    [tenantId, repoId, prId, runId, role, key, status, startedAt, latency, `usage-it-skill-${idemN}`]);
}
async function seedRag({ tenantId, repoId, prId, runId, role, status, latency, resultCount, startedAt }) {
  idemN += 1;
  await pool.query(
    `INSERT INTO mu.rag_retrieval_event
       (tenant_id, repo_id, pr_id, run_id, agent_role, skill_key, query_digest,
        result_count, source_digest_list, status, started_at, completed_at, latency_ms, idempotency_key)
     VALUES ($1,$2,$3,$4,$5,'rag-search',$6,$7,'[]'::jsonb,$8,$9::timestamptz,
             $9::timestamptz + make_interval(secs => 1),$10,$11)`,
    [tenantId, repoId, prId, runId, role, sha256(`q-${idemN}`), resultCount, status,
     startedAt, latency, `usage-it-rag-${idemN}`]);
}
const daysAgo = (n) => new Date(Date.now() - n * 86_400_000);

let T1 = null;
try {
  await new Promise((r) => setTimeout(r, 1500)); // 等 eager schema init

  const admin = await muLogin('fixture:dev-pilot');
  ok('P0 admin（platform_admin）fixture 登录', admin.status === 200 && admin.json?.role === 'platform_admin');
  const sess = await call('/api/mu/session', { cookie: admin.cookie });
  T1 = sess.json?.tenant?.tenant_id ?? null;
  ok('P0b 会话租户可读（bootstrap 租户）', Boolean(T1), sess.json?.tenant);

  // 贡献者 bob（有 read_pull_request）与审计员 harry（仅 read_audit）
  await fetch(BASE + '/api/mu/members', { method: 'POST',
    headers: { 'content-type': 'application/json', cookie: admin.cookie, 'x-csrf-token': admin.csrf },
    body: JSON.stringify({ login: 'bob', role: 'contributor' }) });
  await fetch(BASE + '/api/mu/members', { method: 'POST',
    headers: { 'content-type': 'application/json', cookie: admin.cookie, 'x-csrf-token': admin.csrf },
    body: JSON.stringify({ login: 'harry', role: 'auditor' }) });
  const bob = await muLogin('fixture:bob');
  const harry = await muLogin('fixture:harry');
  ok('P0c contributor/auditor 就位', bob.json?.role === 'contributor' && harry.json?.role === 'auditor');

  // ── T1 种子：repo1（pr1/run1）+ repo1b（pr1b/run1b）+ v2 前"无留痕"对照 run ──
  const repo1 = (await pool.query(
    `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,'github','usage-r1','o','r1') RETURNING repo_id`, [T1])).rows[0].repo_id;
  const pr1 = (await pool.query(
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
     VALUES ($1,$2,101,$3) RETURNING pr_id`, [T1, repo1, sha256('usage-pr1')])).rows[0].pr_id;
  const run1 = (await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
     VALUES ($1,$2,$3,$4,'COMPLETED') RETURNING run_id`, [T1, repo1, pr1, sha256('usage-pr1')])).rows[0].run_id;
  const repo1b = (await pool.query(
    `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,'github','usage-r1b','o','r1b') RETURNING repo_id`, [T1])).rows[0].repo_id;
  const pr1b = (await pool.query(
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
     VALUES ($1,$2,102,$3) RETURNING pr_id`, [T1, repo1b, sha256('usage-pr1b')])).rows[0].pr_id;
  const run1b = (await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
     VALUES ($1,$2,$3,$4,'COMPLETED') RETURNING run_id`, [T1, repo1b, pr1b, sha256('usage-pr1b')])).rows[0].run_id;
  const legacyRun = (await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
     VALUES ($1,$2,$3,$4,'COMPLETED') RETURNING run_id`, [T1, repo1, pr1, sha256('usage-legacy')])).rows[0].run_id;
  ok('P1 T1 实体播种（repo1/pr1/run1 + repo1b/pr1b/run1b + 无留痕对照 run）',
    Boolean(repo1 && pr1 && run1 && repo1b && pr1b && run1b && legacyRun));

  // 手算基准（窗口 30d）：
  //  skill: alpha=3（SUCCEEDED 100ms/300ms + FAILED 200ms，verifier）、
  //         beta=3（SUCCEEDED 50ms + CANCELLED(null) + RUNNING(null)，reviewer）、
  //         delta=1（SUCCEEDED 80ms，leader；repo1b）
  //  rag:   2×SUCCEEDED（rc 3/5，lat 20/60，today）+ 1×FAILED（rc 0，lat 10，yesterday）
  //  90d 另有 gamma=1（TIMEOUT 999ms，fixer，60 天前）
  const today = daysAgo(0); const yesterday = daysAgo(1); const d60 = daysAgo(60);
  await seedSkill({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'verifier', key: 'skill-alpha', status: 'SUCCEEDED', latency: 100, startedAt: today });
  await seedSkill({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'verifier', key: 'skill-alpha', status: 'SUCCEEDED', latency: 300, startedAt: today });
  await seedSkill({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'verifier', key: 'skill-alpha', status: 'FAILED', latency: 200, startedAt: yesterday });
  await seedSkill({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'reviewer', key: 'skill-beta', status: 'SUCCEEDED', latency: 50, startedAt: today });
  await seedSkill({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'reviewer', key: 'skill-beta', status: 'CANCELLED', latency: null, startedAt: today });
  await seedSkill({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'reviewer', key: 'skill-beta', status: 'RUNNING', latency: null, startedAt: today });
  await seedSkill({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'fixer', key: 'skill-gamma', status: 'TIMEOUT', latency: 999, startedAt: d60 });
  await seedSkill({ tenantId: T1, repoId: repo1b, prId: pr1b, runId: run1b, role: 'leader', key: 'skill-delta', status: 'SUCCEEDED', latency: 80, startedAt: today });
  await seedRag({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'leader', status: 'SUCCEEDED', latency: 20, resultCount: 3, startedAt: today });
  await seedRag({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'reviewer', status: 'SUCCEEDED', latency: 60, resultCount: 5, startedAt: today });
  await seedRag({ tenantId: T1, repoId: repo1, prId: pr1, runId: run1, role: 'verifier', status: 'FAILED', latency: 10, resultCount: 0, startedAt: yesterday });
  ok('P2 事件种子写入（8 skill + 3 rag）',
    Number((await pool.query('SELECT count(*)::int c FROM mu.skill_invocation_event WHERE tenant_id=$1', [T1])).rows[0].c) === 8
    && Number((await pool.query('SELECT count(*)::int c FROM mu.rag_retrieval_event WHERE tenant_id=$1', [T1])).rows[0].c) === 3);

  // ── U1：未登录 401 ──
  for (const pp of ['/api/mu/usage/summary', '/api/mu/usage/by-skill', '/api/mu/usage/by-period']) {
    const r = await call(pp);
    ok(`U1 未登录 ${pp} → 401`, r.status === 401, r.status);
  }

  // ── U2：auditor（仅 read_audit）→ 403 action_not_granted ──
  for (const pp of ['/api/mu/usage/summary', '/api/mu/usage/by-skill', '/api/mu/usage/by-period']) {
    const r = await call(pp, { cookie: harry.cookie });
    ok(`U2 auditor ${pp} → 403 action_not_granted`, r.status === 403
      && r.json?.error?.reason === 'action_not_granted', { s: r.status, j: r.json });
  }

  // ── U3：contributor（read_pull_request）→ 200（非管理员也可读） ──
  const bobSum = await call('/api/mu/usage/summary?window=30d', { cookie: bob.cookie });
  ok('U3 contributor usage/summary → 200', bobSum.status === 200, { s: bobSum.status, j: bobSum.json });

  // ── U4：summary 默认 30d——手算核对 ──
  // total=7（alpha3+beta3+delta1；gamma 60d 在窗外）succeeded=4 failed=1 cancelled=1 timeout=0
  // success_rate=4/7=0.5714；非空 latency 样本（5 个）[50,80,100,200,300] →
  // p50=100（第 3 位）、p95=200+0.8*100=280（percentile_cont 线性插值）
  const sum30 = (await call('/api/mu/usage/summary', { cookie: admin.cookie })).json;
  ok('U4a summary.window 默认 30d', sum30?.window === '30d', sum30?.window);
  ok('U4b skill total=7 / succeeded=4 / failed=1 / cancelled=1 / timeout=0',
    sum30?.skill?.total === 7 && sum30?.skill?.succeeded === 4 && sum30?.skill?.failed === 1
    && sum30?.skill?.cancelled === 1 && sum30?.skill?.timeout === 0, sum30?.skill);
  ok('U4c skill success_rate=0.5714（4/7，4 位小数）', sum30?.skill?.success_rate === 0.5714, sum30?.skill?.success_rate);
  ok('U4d skill p50=100 / p95=280（percentile_cont 仅非空 latency，线性插值）',
    sum30?.skill?.latency_p50_ms === 100 && sum30?.skill?.latency_p95_ms === 280,
    { p50: sum30?.skill?.latency_p50_ms, p95: sum30?.skill?.latency_p95_ms });
  ok('U4e skill last_called_at 为 ISO 时间', typeof sum30?.skill?.last_called_at === 'string'
    && !Number.isNaN(Date.parse(sum30.skill.last_called_at)), sum30?.skill?.last_called_at);
  ok('U4f rag total=3 / succeeded=2 / failed=1 / result_count_sum=8 / p50=20 / p95=56',
    sum30?.rag?.total === 3 && sum30?.rag?.succeeded === 2 && sum30?.rag?.failed === 1
    && sum30?.rag?.result_count_sum === 8 && sum30?.rag?.latency_p50_ms === 20
    && sum30?.rag?.latency_p95_ms === 56, sum30?.rag);
  ok('U4g token_metering/cost unavailable 块在合同内',
    sum30?.token_metering?.available === false && sum30?.token_metering?.reason === 'no_token_source'
    && sum30?.cost?.available === false && sum30?.cost?.reason === 'no_price_table', sum30);
  ok('U4h 响应零 digest（无 input/output/query digest、无 source_digest_list）',
    !JSON.stringify(sum30).toLowerCase().includes('digest'), Object.keys(sum30 ?? {}));

  // ── U5：window=90d / all——gamma 计入 ──
  // 90d 非空样本（6 个）[50,80,100,200,300,999] → p50=(100+200)/2=150、
  // p95=300+0.75*699=824（线性插值）
  const sum90 = (await call('/api/mu/usage/summary?window=90d', { cookie: admin.cookie })).json;
  ok('U5a 90d total=8 / timeout=1 / success_rate=0.5 / p50=150 / p95=824',
    sum90?.skill?.total === 8 && sum90?.skill?.timeout === 1
    && sum90?.skill?.success_rate === 0.5 && sum90?.skill?.latency_p50_ms === 150
    && sum90?.skill?.latency_p95_ms === 824, sum90?.skill);
  const sumAll = (await call('/api/mu/usage/summary?window=all', { cookie: admin.cookie })).json;
  ok('U5b window=all 合法且 total=8', sumAll?.window === 'all' && sumAll?.skill?.total === 8, sumAll?.skill);

  // ── U6：过滤参数各自生效 ──
  const fSkill = (await call('/api/mu/usage/summary?window=30d&skill_key=skill-alpha', { cookie: admin.cookie })).json;
  ok('U6a skill_key=skill-alpha → total=3 / succeeded=2 / p50=200 / p95=290',
    fSkill?.skill?.total === 3 && fSkill?.skill?.succeeded === 2
    && fSkill?.skill?.latency_p50_ms === 200 && fSkill?.skill?.latency_p95_ms === 290, fSkill?.skill);
  ok('U6b filters 回显请求过滤（skill_key）', fSkill?.filters?.skill_key === 'skill-alpha'
    && fSkill?.filters?.agent_role === null, fSkill?.filters);
  const fRole = (await call('/api/mu/usage/summary?window=30d&agent_role=reviewer', { cookie: admin.cookie })).json;
  ok('U6c agent_role=reviewer → skill total=3 / succeeded=1（beta 域）',
    fRole?.skill?.total === 3 && fRole?.skill?.succeeded === 1, fRole?.skill);
  const fStatus = (await call('/api/mu/usage/summary?window=30d&status=SUCCEEDED', { cookie: admin.cookie })).json;
  ok('U6d status=SUCCEEDED → skill total=4 / rag total=2',
    fStatus?.skill?.total === 4 && fStatus?.rag?.total === 2, { s: fStatus?.skill?.total, r: fStatus?.rag?.total });
  const fRepo1 = (await call(`/api/mu/usage/summary?window=30d&repo_id=${repo1}`, { cookie: admin.cookie })).json;
  ok('U6e repo_id=repo1 → total=6（delta 在 repo1b 不计入）',
    fRepo1?.skill?.total === 6, fRepo1?.skill);
  const fRepo1b = (await call(`/api/mu/usage/summary?window=30d&repo_id=${repo1b}`, { cookie: admin.cookie })).json;
  ok('U6f repo_id=repo1b → total=1 / succeeded=1', fRepo1b?.skill?.total === 1
    && fRepo1b?.skill?.succeeded === 1, fRepo1b?.skill);
  const fPr1 = (await call(`/api/mu/usage/summary?window=30d&pr_id=${pr1}`, { cookie: admin.cookie })).json;
  ok('U6g pr_id=pr1 → total=6（repo1b 的 pr1b 不计入）', fPr1?.skill?.total === 6, fPr1?.skill);
  const fEmpty = (await call(`/api/mu/usage/summary?window=30d&repo_id=${repo1b}&skill_key=skill-alpha`, { cookie: admin.cookie })).json;
  ok('U6h 组合过滤零命中 → total=0 / success_rate=null / p50=null / last_called_at=null',
    fEmpty?.skill?.total === 0 && fEmpty?.skill?.success_rate === null
    && fEmpty?.skill?.latency_p50_ms === null && fEmpty?.skill?.last_called_at === null, fEmpty?.skill);

  // ── U7：非法参数 400 invalid_filter ──
  const badCases = [
    ['window=60d', 'window'],
    ['status=BOGUS', 'status'],
    ['agent_role=ceo', 'agent_role'],
    ['skill_key=BAD_KEY', 'skill_key'],
    ['pr_id=not-a-uuid', 'pr_id'],
  ];
  for (const [qs, field] of badCases) {
    const r = await call(`/api/mu/usage/summary?${qs}`, { cookie: admin.cookie });
    ok(`U7 summary?${qs} → 400 invalid_filter(${field})`, r.status === 400
      && r.json?.error?.reason === 'invalid_filter' && r.json?.error?.fields?.includes(field),
      { s: r.status, j: r.json });
  }
  const badPeriodAll = await call('/api/mu/usage/by-period?window=all', { cookie: admin.cookie });
  ok('U7f by-period window=all → 400（限 7d/30d/90d）', badPeriodAll.status === 400
    && badPeriodAll.json?.error?.fields?.includes('window'), badPeriodAll.json);

  // ── U8：by-skill 分组/排序/分页/总组数 ──
  const bySkill30 = (await call('/api/mu/usage/by-skill?window=30d', { cookie: admin.cookie })).json;
  ok('U8a by-skill 30d：total_groups=3 / 默认 limit=20',
    bySkill30?.total_groups === 3 && bySkill30?.limit === 20, { g: bySkill30?.total_groups, l: bySkill30?.limit });
  ok('U8b by-skill 30d 分组值：alpha(3/2/1) / beta(3/1/0) / delta(1/1/0)',
    bySkill30?.rows?.length === 3
    && bySkill30.rows[0]?.skill_key === 'skill-alpha' && bySkill30.rows[0]?.total === 3
    && bySkill30.rows[0]?.succeeded === 2 && bySkill30.rows[0]?.failed === 1
    && bySkill30.rows[1]?.skill_key === 'skill-beta' && bySkill30.rows[1]?.succeeded === 1
    && bySkill30.rows[1]?.cancelled === undefined && bySkill30.rows[1]?.total === 3
    && bySkill30.rows[2]?.skill_key === 'skill-delta' && bySkill30.rows[2]?.total === 1,
    bySkill30?.rows);
  ok('U8c 排序=total DESC, skill_key ASC（alpha 先于 beta）',
    bySkill30?.rows?.[0]?.skill_key === 'skill-alpha' && bySkill30?.rows?.[1]?.skill_key === 'skill-beta',
    bySkill30?.rows?.map((r) => r.skill_key));
  const bySkill90 = (await call('/api/mu/usage/by-skill?window=90d', { cookie: admin.cookie })).json;
  ok('U8d by-skill 90d：total_groups=4，同 total 按 key 升序（delta 先于 gamma）',
    bySkill90?.total_groups === 4
    && JSON.stringify(bySkill90?.rows?.map((r) => r.skill_key))
      === JSON.stringify(['skill-alpha', 'skill-beta', 'skill-delta', 'skill-gamma']),
    bySkill90?.rows?.map((r) => r.skill_key));
  const pg1 = (await call('/api/mu/usage/by-skill?window=90d&limit=2&offset=0', { cookie: admin.cookie })).json;
  const pg2 = (await call('/api/mu/usage/by-skill?window=90d&limit=2&offset=2', { cookie: admin.cookie })).json;
  ok('U8e 分页 limit=2：页一 alpha/beta、页二 delta/gamma、total_groups 恒 4',
    JSON.stringify(pg1?.rows?.map((r) => r.skill_key)) === JSON.stringify(['skill-alpha', 'skill-beta'])
    && JSON.stringify(pg2?.rows?.map((r) => r.skill_key)) === JSON.stringify(['skill-delta', 'skill-gamma'])
    && pg1?.total_groups === 4 && pg2?.total_groups === 4,
    { p1: pg1?.rows?.map((r) => r.skill_key), p2: pg2?.rows?.map((r) => r.skill_key) });
  const bsStatus = (await call('/api/mu/usage/by-skill?window=30d&status=SUCCEEDED', { cookie: admin.cookie })).json;
  ok('U8f status 过滤先于分组：alpha=2 / beta=1 / delta=1',
    bsStatus?.rows?.find((r) => r.skill_key === 'skill-alpha')?.total === 2
    && bsStatus?.rows?.find((r) => r.skill_key === 'skill-beta')?.total === 1
    && bsStatus?.rows?.find((r) => r.skill_key === 'skill-delta')?.total === 1, bsStatus?.rows);

  // ── U9：by-period 按日分组（UTC；无数据日不补零） ──
  const bp30 = (await call('/api/mu/usage/by-period?window=30d', { cookie: admin.cookie })).json;
  ok('U9a by-period 30d：恰 2 个真实数据日、升序',
    bp30?.rows?.length === 2 && bp30.rows[0].day < bp30.rows[1].day, bp30?.rows);
  ok('U9b 昨日行：skill_total=1 / skill_succeeded=0 / rag_total=1',
    bp30?.rows?.[0]?.skill_total === 1 && bp30?.rows?.[0]?.skill_succeeded === 0
    && bp30?.rows?.[0]?.rag_total === 1, bp30?.rows?.[0]);
  ok('U9c 今日行：skill_total=6 / skill_succeeded=4 / rag_total=2',
    bp30?.rows?.[1]?.skill_total === 6 && bp30?.rows?.[1]?.skill_succeeded === 4
    && bp30?.rows?.[1]?.rag_total === 2, bp30?.rows?.[1]);
  const bp90 = (await call('/api/mu/usage/by-period?window=90d', { cookie: admin.cookie })).json;
  ok('U9d by-period 90d：3 个数据日，60 天前行 skill_total=1 / rag_total=0（真实侧零，非补零日）',
    bp90?.rows?.length === 3 && bp90.rows[0].skill_total === 1 && bp90.rows[0].rag_total === 0, bp90?.rows);
  const bp7 = (await call('/api/mu/usage/by-period?window=7d', { cookie: admin.cookie })).json;
  ok('U9e by-period 7d：与 30d 同（种子均落在 1 天内）',
    bp7?.rows?.length === 2, bp7?.rows?.length);

  // ── U10：v2 前"无留痕"对照——历史 run 不出现在统计是正确语义 ──
  const legacySummary = (await call(`/api/mu/usage/summary?window=all`, { cookie: admin.cookie })).json;
  ok('U10a 无留痕对照 run 不产生任何计数（总量仍=手算值，不虚构）',
    legacySummary?.skill?.total === 8 && legacySummary?.rag?.total === 3, legacySummary?.skill);
  const legacyCall = (await call(`/api/mu/runs/${legacyRun}/call-summary`, { cookie: admin.cookie })).json;
  ok('U10b 对照 run 的 call-summary 恒 0（无留痕=无该 run 计数，而非租户零用量）',
    legacyCall?.skill?.total === 0 && legacyCall?.rag?.total === 0, legacyCall);

  // ── U11：跨租户隔离（第二租户种子行对第一租户不可见） ──
  const T2 = (await pool.query(
    `INSERT INTO mu.tenant (slug, display_name) VALUES ('usage-t2','USAGE-T2') RETURNING tenant_id`)).rows[0].tenant_id;
  const t2user = (await pool.query(
    `INSERT INTO mu.app_user (login, display_name) VALUES ('t2boss','T2 Boss') RETURNING user_id`)).rows[0].user_id;
  await pool.query(
    `INSERT INTO mu.membership (tenant_id, user_id, role, state) VALUES ($1,$2,'platform_admin','active')`, [T2, t2user]);
  await pool.query(
    `INSERT INTO mu.external_identity (user_id, provider, subject) VALUES ($1,'fixture','fixture:t2boss')`, [t2user]);
  const repo2 = (await pool.query(
    `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,'github','usage-r2','o2','r2') RETURNING repo_id`, [T2])).rows[0].repo_id;
  const pr2 = (await pool.query(
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
     VALUES ($1,$2,201,$3) RETURNING pr_id`, [T2, repo2, sha256('usage-pr2')])).rows[0].pr_id;
  const run2 = (await pool.query(
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, status)
     VALUES ($1,$2,$3,$4,'COMPLETED') RETURNING run_id`, [T2, repo2, pr2, sha256('usage-pr2')])).rows[0].run_id;
  await seedSkill({ tenantId: T2, repoId: repo2, prId: pr2, runId: run2, role: 'verifier', key: 'skill-t2-only', status: 'SUCCEEDED', latency: 5, startedAt: daysAgo(0) });
  await seedRag({ tenantId: T2, repoId: repo2, prId: pr2, runId: run2, role: 'leader', status: 'SUCCEEDED', latency: 5, resultCount: 1, startedAt: daysAgo(0) });
  const t2 = await muLogin('fixture:t2boss');
  ok('U11a T2 管理员登录', t2.status === 200 && t2.json?.tenant?.tenant_id === T2, t2.json?.tenant);
  const t2sum = (await call('/api/mu/usage/summary?window=all', { cookie: t2.cookie })).json;
  ok('U11b T2 只见自己的 1 skill + 1 rag（零泄漏）',
    t2sum?.skill?.total === 1 && t2sum?.rag?.total === 1, t2sum);
  const t2bySkill = (await call('/api/mu/usage/by-skill?window=all', { cookie: t2.cookie })).json;
  ok('U11c T2 by-skill 不含 T1 技能键',
    t2bySkill?.rows?.length === 1 && t2bySkill.rows[0].skill_key === 'skill-t2-only', t2bySkill?.rows);
  const t1cross = await call(`/api/mu/usage/summary?window=30d&repo_id=${repo2}`, { cookie: admin.cookie });
  ok('U11d T1 引用 T2 repo_id → 404 repository_not_found', t1cross.status === 404
    && t1cross.json?.error?.reason === 'repository_not_found', { s: t1cross.status, j: t1cross.json });
  const t2cross = await call(`/api/mu/usage/summary?window=30d&repo_id=${repo1}`, { cookie: t2.cookie });
  ok('U11e T2 引用 T1 repo_id → 404', t2cross.status === 404
    && t2cross.json?.error?.reason === 'repository_not_found', t2cross.status);
  const t1After = (await call('/api/mu/usage/summary?window=all', { cookie: admin.cookie })).json;
  ok('U11f T2 种子后 T1 总量不变（8 skill / 3 rag）',
    t1After?.skill?.total === 8 && t1After?.rag?.total === 3, t1After);

  // ── U12：未知 usage 子路径 → 404（无聚合面扩权） ──
  const unknown = await call('/api/mu/usage/bogus', { cookie: admin.cookie });
  ok('U12 /api/mu/usage/bogus → 404', unknown.status === 404, unknown.status);
} catch (e) {
  fail++; console.error('HARNESS ERROR', e);
} finally {
  server.close();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
