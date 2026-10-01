// console/backend/test/mu-purify-w37.integration.mjs — Wave 3.7 数据源纯化门槛。
//
// 证明（MU_MODE=multiuser 下）：
//   P1 fxv 漂移调和 migration v13 七场景（fresh/漂移空表/合法 text/非法 text/重复/回滚重放/ABSENT capability）
//   P2 legacy 哨兵：可识别哨兵行只存在于 legacy 表——全部 facade 响应零哨兵字符串
//   P3 zero-legacy-query：MU_QUERY_TRACE 证明 MU 请求零查询 skill_receipt_outbox/approval/skill_gate_audit/fxv
//   P4 响应契约：业务 facade data_source=MU_CANONICAL_LIVE + as_of + schema_version；零 mixed-source
//   P5 /api/pulls/:n 编号寻址（MU canonical，contract 形状）
// 诊断输出（trace/access log）写入临时文件，finally 清理——不提交任何含敏感输出的文件。
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

// ── 一次性 postgres（mu-rbac 同款；随机回环端口；finally 强制清理） ──
const CTR = `mu-purify-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16400 + Math.floor(Math.random() * 80);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const adminPool = new Pool({ connectionString: dsn });
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { await adminPool.query('SELECT 1'); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'mu-purify-'));
const TRACE = path.join(TMP, 'query-trace.jsonl');
const ACCESS = path.join(TMP, 'access-log.jsonl');

// ── P1a：漂移表预置（早期波次遗留形状：BIGSERIAL 主键 + TEXT state_detail + TEXT payload） ──
await adminPool.query(`CREATE SCHEMA IF NOT EXISTS fxv`);
await adminPool.query(`CREATE TABLE fxv.attempts (
  attempt_id BIGSERIAL PRIMARY KEY, run_id text NOT NULL DEFAULT '', repo text NOT NULL DEFAULT '',
  head_sha text, status text NOT NULL DEFAULT 'PENDING', finding_count int DEFAULT 0,
  duration_ms bigint, payload text, state_detail text,
  created_at timestamptz NOT NULL DEFAULT now())`);
await adminPool.query(`INSERT INTO fxv.attempts (repo, status, payload, state_detail) VALUES
  ('legacy/repo', 'DONE', '{"p":1}', '{"k":1}'),
  ('legacy/repo', 'DONE', 'not-valid-json{', 'not-valid-json{'),
  ('legacy/repo', 'DONE', '', '')`);

const savedEnv = { ...process.env };
process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'mu-purify-session-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';
process.env.MU_FIXTURES = '1';
process.env.MU_QUERY_TRACE = TRACE;
process.env.MU_ACCESS_LOG = ACCESS;

try {
  // initSchema：v1..v13 全量迁移（漂移表在 v13 被调和）
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const store = await createMuStore({ pool: adminPool });
  await store.initSchema();
  await store.bootstrap();

  // P1b：调和结果断言
  const cols = (await adminPool.query(
    `SELECT column_name, data_type FROM information_schema.columns
      WHERE table_schema='fxv' AND table_name='attempts' ORDER BY ordinal_position`)).rows;
  const colType = (n) => cols.find((c) => c.column_name === n)?.data_type;
  ok('P1b-1 attempt_id BIGINT→TEXT', colType('attempt_id') === 'text', colType('attempt_id'));
  ok('P1b-2 state_detail TEXT→JSONB', colType('state_detail') === 'jsonb', colType('state_detail'));
  ok('P1b-3 payload TEXT→JSONB（旧 text payload 兼容）', colType('payload') === 'jsonb', colType('payload'));
  const rows = (await adminPool.query(
    `SELECT state_detail, payload FROM fxv.attempts ORDER BY attempt_id`)).rows;
  ok('P1b-4 text→jsonb 逐行容错（合法保留/非法→{}/空串→{}）',
    rows[0]?.state_detail?.k === 1 && rows[1]?.state_detail
      && Object.keys(rows[1].state_detail).length === 0
      && rows[2]?.state_detail && Object.keys(rows[2].state_detail).length === 0, rows);
  ok('P1b-4b payload 转换同步成立（合法 JSON 保留）', rows[0]?.payload?.p === 1, rows[0]?.payload);
  ok('P1b-5 fxv.audit_events 补齐（读路径依赖）',
    (await adminPool.query(`SELECT to_regclass('fxv.audit_events') r`)).rows[0].r !== null);

  // P1c：fresh DB 场景（无 fxv schema）→ 迁移跳过 + 读 API 返回 ABSENT capability（非 BACKEND_ERROR）
  const CTR2 = `${CTR}-fresh`;
  const PGPORT2 = PGPORT + 1;
  execFileSync('docker', ['run', '-d', '--name', CTR2,
    '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PGPORT2}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  const dsn2 = `postgres://postgres:x@127.0.0.1:${PGPORT2}/mu`;
  const p2 = new Pool({ connectionString: dsn2 });
  for (let i = 0; i < 60; i++) { try { await p2.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }
  const store2 = await createMuStore({ pool: p2 });
  await store2.initSchema();
  await store2.bootstrap();
  const freshCols = await p2.query(`SELECT to_regclass('fxv.attempts') r`).catch(() => ({ rows: [{ r: null }] }));
  ok('P1c-1 fresh DB 迁移不抢建 fxv 主表（DDL 单一权威=fxv store）', freshCols.rows[0].r === null);
  const { fxvMetrics } = await import('../lib/fxv/metrics.mjs');
  const pgReal = createRequire(path.join(HERE, 'support/noop.js'))('pg');
  const absent = await fxvMetrics(dsn2, { pgImpl: pgReal });
  ok('P1c-2 fresh DB fxvMetrics=FXV_PERSISTENCE_ABSENT capability（非错误态）',
    absent.data_source === 'FXV_PERSISTENCE_ABSENT' && absent.capability === 'fxv_persistence_not_initialized', absent);
  await p2.end();

  // P1d：重复 migration / 回滚重放（全守卫语句可重放）
  await adminPool.query(`DELETE FROM mu.schema_migrations WHERE version=13`);
  await store.initSchema();
  const cols2 = (await adminPool.query(
    `SELECT data_type FROM information_schema.columns WHERE table_schema='fxv'
      AND table_name='attempts' AND column_name='state_detail'`)).rows;
  ok('P1d 回滚重放幂等（v13 重放后仍 JSONB）', cols2[0]?.data_type === 'jsonb');

  // ── P2/P3/P4：起 console（trace+access log 开启），seed 哨兵与 MU 数据 ──
  const { createConsole } = await import('../server.mjs');
  const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const BASE = `http://127.0.0.1:${server.address().port}`;

  // legacy 哨兵表（可识别行——任何 legacy 读取都会把它带出）
  await adminPool.query(`CREATE SCHEMA IF NOT EXISTS approval`);
  await adminPool.query(`CREATE TABLE IF NOT EXISTS approval.tickets (
    ticket_id text PRIMARY KEY, run_id text, repo_id text, pr_number int, head_sha text,
    action text, status text, finding_id text, finding_fingerprint text, target_key text,
    created_at timestamptz DEFAULT now(), approval_expires_at timestamptz)`);
  await adminPool.query(`CREATE TABLE IF NOT EXISTS skill_receipt_outbox (
    id bigserial PRIMARY KEY, payload jsonb NOT NULL, created_at timestamptz DEFAULT now())`);
  await adminPool.query(`CREATE TABLE IF NOT EXISTS skill_gate_audit (
    id bigserial PRIMARY KEY, run_id text, decision jsonb, created_at timestamptz DEFAULT now())`);
  await adminPool.query(`INSERT INTO approval.tickets
    (ticket_id, run_id, repo_id, pr_number, action, status) VALUES
    ('SENTINEL-TICKET-legacy-only', 'SENTINEL-RUN', 'legacy-sentinel/repo', 999, 'approve', 'PENDING')`);
  await adminPool.query(`INSERT INTO skill_receipt_outbox (payload) VALUES
    ('{"repo":"legacy-sentinel/repo","pr":888,"run_id":"SENTINEL-RUN","head_sha":"sentinel0"}')`);
  await adminPool.query(`INSERT INTO skill_gate_audit (run_id, decision) VALUES
    ('SENTINEL-RUN', '{"verdict":"SENTINEL-VERDICT"}')`);

  // MU 会话 + canonical 数据（tenant A）
  async function muLogin(subject) {
    const res = await fetch(BASE + '/api/mu/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ provider: 'fixture', subject }) });
    const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
    let json = null; try { json = await res.json(); } catch { /* */ }
    return { cookie, csrf: json?.csrf ?? null };
  }
  const admin = await muLogin('fixture:dev-pilot'); // bootstrap 操作员→platform_admin+default tenant
  const H = { cookie: admin.cookie, 'content-type': 'application/json', 'x-csrf-token': admin.csrf ?? '' };

  const repo = await store.ensureRepository({ tenantId: (await adminPool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id,
    provider: 'github', providerRepoId: '7001', owner: 'acme', name: 'app', defaultBranch: 'main' });
  const T = (await adminPool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  const pr = await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: 42,
    headSha: 'c0ffee42', title: 'purify pr' });

  // trace 窗口标记：迁移/哨兵播种自身的查询不计入——只审「请求期」查询
  const traceMark = fs.existsSync(TRACE) ? fs.readFileSync(TRACE, 'utf8').split('\n').length : 0;

  // ── 逐 facade 请求（业务面）──
  const facadeChecks = [
    ['/api/overview', 'MU_CANONICAL_LIVE'],
    ['/api/pulls', 'MU_CANONICAL_LIVE'],
    ['/api/pending', 'MU_CANONICAL_LIVE'],
    ['/api/tickets', 'MU_CANONICAL_LIVE'],
    ['/api/evidence', 'MU_CANONICAL_LIVE'],
    ['/api/audit', 'MU_CANONICAL_LIVE'],
    ['/api/runs?limit=200', 'MU_CANONICAL_LIVE'],
    [`/api/pulls/42?repo=acme/app`, 'MU_CANONICAL_LIVE'],
  ];
  const sentinel = 'legacy-sentinel';
  for (const [url, ds] of facadeChecks) {
    const r = await fetch(BASE + url, { headers: { cookie: admin.cookie } });
    const body = await r.json().catch(() => null);
    const text = JSON.stringify(body ?? {});
    ok(`P2 ${url} 零 legacy 哨兵`, !text.includes(sentinel) && !text.includes('SENTINEL'), url);
    ok(`P4 ${url} data_source=${ds} + as_of + schema_version`,
      body?.data_source === ds && typeof body?.as_of === 'string' && typeof body?.schema_version === 'string',
      { ds: body?.data_source, as_of: body?.as_of });
  }

  // P5：pulls/:n contract 形状 + 编号寻址
  {
    const r = await fetch(BASE + '/api/pulls/42?repo=acme/app', { headers: { cookie: admin.cookie } });
    const b = await r.json().catch(() => null);
    ok('P5-1 编号寻址 200 + repo/pr_number 字段',
      r.status === 200 && b?.repo === 'acme/app' && Number(b?.pr_number) === 42,
      { status: r.status, repo: b?.repo, pr_number: b?.pr_number });
    ok('P5-2 contract 形状（无 run→latest_result=null；merge_panel fail-closed）',
      b?.latest_result === null && b?.merge_panel?.enabled === false
        && b?.stage_source === 'mu_pull_request' && b?.current_head_sha === 'c0ffee42',
      { lr: b?.latest_result, mp: b?.merge_panel?.enabled, head: b?.current_head_sha });
    const r404 = await fetch(BASE + '/api/pulls/9999?repo=acme/app', { headers: { cookie: admin.cookie } });
    ok('P5-3 不存在 PR → 404 no_live_record（不回退 legacy）', r404.status === 404);
  }

  // P2 附：fxv 能力面（MU 下零全局聚合）
  {
    const r = await fetch(BASE + '/api/fxv/metrics', { headers: { cookie: admin.cookie } });
    const b = await r.json().catch(() => null);
    ok('P2-fxv metrics=UNSCOPED capability + 空 metrics（零跨租户聚合）',
      r.status === 200 && b?.data_source === 'FXV_PERSISTENCE_UNSCOPED' && b?.metrics && Object.keys(b.metrics).length === 0, b);
    const r2 = await fetch(BASE + '/api/fxv/attempts', { headers: { cookie: admin.cookie } });
    const b2 = await r2.json().catch(() => null);
    ok('P2-fxv attempts=UNSCOPED capability + 空列表',
      r2.status === 200 && b2?.data_source === 'FXV_PERSISTENCE_UNSCOPED' && Array.isArray(b2?.attempts) && b2.attempts.length === 0, b2);
  }

  // P3：trace 证据——MU 请求零 legacy 表查询（含 fxv——capability 分支不落库）
  await new Promise((r) => setTimeout(r, 300));
  const traceLines = fs.existsSync(TRACE)
    ? fs.readFileSync(TRACE, 'utf8').trim().split('\n').filter(Boolean)
        .map((l) => JSON.parse(l)).slice(traceMark) : [];
  const touched = new Set(traceLines.flatMap((l) => l.tables ?? []));
  const LEGACY = ['skill_receipt_outbox', 'approval.tickets', 'skill_gate_audit', 'fxv.attempts', 'fxv.audit_events'];
  ok('P3 zero-legacy-query：trace 零 legacy/fxv 表命中',
    LEGACY.every((t) => !touched.has(t)), { touched: [...touched] });
  ok('P3-trace 结构：仅表名+调用点（无 SQL 文本/参数）',
    traceLines.every((l) => Array.isArray(l.tables) && typeof l.caller === 'string' && !('sql' in l) && !('params' in l)));
  ok('P3-trace 全部 mu.* canonical',
    [...touched].every((t) => t.startsWith('mu.')), { touched: [...touched] });

  // P4 附：access log=等价网络清单（结构+data_source 单一值）
  const accessLines = fs.existsSync(ACCESS)
    ? fs.readFileSync(ACCESS, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : [];
  ok('P4-access log 覆盖本轮请求且记录 data_source',
    accessLines.length >= facadeChecks.length
      && accessLines.every((l) => 'path' in l && 'status' in l && 'data_source' in l),
    { n: accessLines.length });

  server.close();
  // fresh DB 清理
  execFileSync('docker', ['rm', '-f', '-v', CTR2], { stdio: 'pipe' });
} finally {
  Object.assign(process.env, savedEnv);
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* */ }
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await adminPool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
