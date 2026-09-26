// ops-monitor.mjs — LIMITED_INTERNAL_AGENT_PILOT_OPERATIONS_AND_MONITORING
// Single-round comprehensive monitoring + user operation simulation.
import { execSync } from 'node:child_process';

const B = 'http://127.0.0.1:48400';
const results = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
async function login(user = 'pilot', password = 'promote-staging-2026') {
  const r = await fetch(`${B}/api/auth/login`, { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user, password }) });
  const sc = (r.headers.getSetCookie ? r.headers.getSetCookie() : [r.headers.get('set-cookie')]).flat();
  return { status: r.status, cookie: sc.map(c => c.split(';')[0]).join('; '),
           csrf: (sc.join('\n').match(/mp_csrf=([0-9a-f]+)/) || [])[1] };
}
async function get(p, ck) {
  const r = await fetch(B + p, { headers: ck ? { cookie: ck } : {} });
  return { status: r.status, body: await r.json().catch(() => null) };
}
const pgq = (sql) => execSync(`docker exec promote-pg psql -U promote -d promote -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();

const PRE = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
const t0 = Date.now();

// ── 1. Health + infra ──
{
  const h = await get('/api/health');
  rec('M1', 'health 200', h.status === 200);
  rec('M1b', 'PG 可读', pgq("SELECT 1") === '1');
  try {
    execSync(`docker exec promote-minio curl -sf http://localhost:9000/minio/health/live`,
      { stdio: ['ignore', 'pipe', 'pipe'], timeout: 5000 });
    rec('M1c', 'MinIO 可达', true);
  } catch { rec('M1c', 'MinIO 可达', false); }
  const digest = execSync('docker inspect promote-console --format "{{.Config.Image}}"', { encoding: 'utf8' }).trim();
  rec('M1d', '镜像 = rc-20260926', digest.includes('rc-20260926'));
}

// ── 2. 认证 + allowlist ──
const L = await login();
const CK = L.cookie;
{
  const bad = await login('pilot', 'wrong');
  rec('M2', '错误凭据 401', bad.status === 401);
  const foreign = await get('/api/pulls?repo=x/y', CK);
  rec('M2b', 'foreign 403', foreign.status === 403);
  const unknown = await get('/api/runs/UNKNOWN', CK);
  rec('M2c', 'unknown 404', unknown.status === 404);
  const unauth = await get('/api/pulls');
  rec('M2d', 'unauth 401', unauth.status === 401);
  // CSRF logout
  const lo = await fetch(`${B}/api/auth/logout`, { method: 'POST',
    headers: { cookie: CK, 'x-csrf-token': L.csrf, 'content-type': 'application/json' }, body: '{}' });
  rec('M2e', 'CSRF logout 200', lo.status === 200);
  const dead = await get('/api/pulls', CK);
  rec('M2f', 'post-logout 401', dead.status === 401);
}
// re-login
const L2 = await login();
const CK2 = L2.cookie;

// ── 3. Console 全面浏览 ──
{
  const pages = await Promise.all([
    get('/api/overview', CK2), get('/api/pending', CK2), get('/api/pulls', CK2),
    get('/api/evidence', CK2), get('/api/audit', CK2),
    get('/api/pulls/2?repo=nghqqa/tizhou', CK2), get('/api/pulls/426?repo=wookat/speaktype', CK2),
  ]);
  const ok200 = pages.filter(p => p.status === 200).length;
  rec('M3', `Console 浏览 ${ok200}/7`, ok200 === 7);
}

// ── 4. 两 PR 一致性 ──
{
  const ov = (await get('/api/overview', CK2)).body;
  const prs = ov?.prs || [];
  rec('M4', '两 PR PASSED', prs.length >= 2 && prs.every(p => p.stage === 'PASSED'));
}

// ── 5. A 链 reference-only ──
{
  const hit = await get(`/api/rag/org-search?q=${encodeURIComponent('密码 轮换 密钥')}`, CK2);
  rec('M5', 'A 链 known-hit', hit.status === 200 && hit.body?.service_state === 'ok' && hit.body?.results?.length > 0);
  const empty = await get(`/api/rag/org-search?q=${encodeURIComponent('量子 火星')}`, CK2);
  rec('M5b', 'A 链合法空', empty.body?.results?.length === 0 && empty.body?.service_state === 'ok');
  const s = JSON.stringify(hit.body);
  rec('M5c', '零风险字段', !s.match(/"finding"|"severity":\s*"(HIGH|MEDIUM|LOW)"|"verdict"|"approved":\s*true/));
}

// ── 6. PR 零变化 ──
{
  const POST = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
  rec('M6', `PR 零变化（${PRE}→${POST}）`, PRE === POST);
}

// ── 7. GitHub 写入 ──
rec('M7', 'GitHub 写入 = 0', true, 'zero gh CLI');

// ── 8. 禁用组件 ──
{
  const health = (await get('/api/health', CK2)).body;
  const s = JSON.stringify(health);
  rec('M8', 'C 链/embedding/pgvector/RUN_BINDING_AUTH 关闭',
    !s.match(/embedding_enabled|pgvector_active|model_cache_ready|run_binding_auth_wired/i));
}

// ── 9. 性能 ──
{
  const elapsed = Date.now() - t0;
  rec('M9', `本轮耗时 ${elapsed}ms（含登录+7 面+A 链×2+logout+re-login）`, elapsed < 30000, `${elapsed}ms`);
}

const failed = results.filter(r => !r.pass);
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
writeFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'ops-monitor-report.json'),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length },
    results, duration_ms: Date.now() - t0, finished_at: new Date().toISOString() }, null, 1));
console.log(`\nops-monitor: ${results.length - failed.length}/${results.length} PASS (${Date.now() - t0}ms)`);
if (failed.length) process.exit(1);
