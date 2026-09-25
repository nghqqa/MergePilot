// CANONICAL_READONLY_OPERATIONS — named-operator drill over the CANONICAL
// console (48190). Items 1-5 via HTTP; skips recorded explicitly.
import { execSync } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const BASE = 'http://127.0.0.1:48190';
const CREDS = { user: 'pilot', password: 'pilot-read-only-2026' };
const results = [], skips = [], events = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass });
  events.push({ id, name, pass, detail: String(detail ?? '') });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
function skip(id, name, reason) {
  skips.push({ id, name, reason });
  console.log(`SKIP [${id}] ${name} — ${reason}`);
}
async function raw(path, init) {
  const r = await fetch(BASE + path, init);
  let json = null; try { json = await r.json(); } catch { /* non-json */ }
  return { status: r.status, json, headers: r.headers };
}
async function login() {
  const r = await raw('/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(CREDS) });
  const cookies = r.headers.get('set-cookie') || '';
  return { status: r.status, cookie: cookies.split(/,(?=\s*mp_)/).map(c => c.split(';')[0]).join('; '),
           csrf: (cookies.match(/mp_csrf=([0-9a-f]+)/) || [])[1] || '' };
}
const q = (sql) => execSync(`docker exec mp-cc-pg psql -U mpcc -d mpcc -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
const py = (code) => execSync(`python -X utf8 -c "${code}"`,
  { encoding: 'utf8', cwd: 'D:/goai/mp-worktrees/integration', stdio: ['ignore', 'pipe', 'pipe'], timeout: 120000 }).trim();

// ── 1 login lifecycle ─────────────────────────────────────────────────
const bad = await raw('/api/auth/login', { method: 'POST',
  headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'wrong' }) });
const L = await login();
const H = { cookie: L.cookie };
const sess = await raw('/api/auth/session', { headers: H });
rec('O1', 'login: bad creds 401 → success 200 → session echo', bad.status === 401 && L.status === 200
  && sess.json?.user === 'pilot' && sess.json?.repos?.length === 2, `${bad.status}/${L.status}/${sess.status}`);
{
  const lo = await raw('/api/auth/logout', { method: 'POST', headers: { ...H, 'x-csrf-token': L.csrf } });
  const dead = await raw('/api/pulls', { headers: H });
  rec('O2', 'logout (with CSRF) invalidates session', lo.status === 200 && dead.status === 401, `${lo.status}/${dead.status}`);
}
const L2 = await login();
const H2 = { cookie: L2.cookie };

// ── 2 /core data surface ──────────────────────────────────────────────
{
  const [p, pn, tk, ev, au] = await Promise.all([
    raw('/api/pulls', { headers: H2 }), raw('/api/pending', { headers: H2 }),
    raw('/api/tickets', { headers: H2 }), raw('/api/evidence', { headers: H2 }),
    raw('/api/audit', { headers: H2 }),
  ]);
  const tz = (p.json?.pulls || []).find((x) => x.repo === 'nghqqa/tizhou');
  const st = (p.json?.pulls || []).find((x) => x.repo === 'wookat/speaktype');
  rec('O3', '/core surface: LIVE + both PRs + heads + receipts + gate audit',
    [p, pn, tk, ev].every(r => r.json?.source === 'POSTGRESQL_LIVE')
      && au.json?.core_source === 'POSTGRESQL_LIVE'
      && tz?.head_sha === 'c443150958b3b8bab0a7b85c907a74ef59206ca5' && tz?.pr_number === 2
      && st?.head_sha === 'dc425e12f683b7a1c96c1d97a5f82c2d7376e257' && st?.pr_number === 426
      && (ev.json?.evidence || []).length >= 4 && (au.json?.gate_decisions || []).length >= 2,
    `evidence=${ev.json?.evidence?.length} gates=${au.json?.gate_decisions?.length}`);
}
// ── 3 allowlist + isolation ────────────────────────────────────────────
{
  const allow = new Set(['wookat/speaktype', 'nghqqa/tizhou']);
  const [p, pn, tk] = await Promise.all([
    raw('/api/pulls', { headers: H2 }), raw('/api/pending', { headers: H2 }),
    raw('/api/tickets', { headers: H2 }),
  ]);
  const rows = [...(p.json?.pulls || []), ...(pn.json?.pending || []), ...(tk.json?.tickets || [])];
  const leaks = rows.filter((x) => !allow.has(x.repo)).length;
  const perSt = await raw('/api/pulls?repo=wookat/speaktype', { headers: H2 });
  const perTz = await raw('/api/pulls?repo=nghqqa/tizhou', { headers: H2 });
  rec('O4', 'allowlist: zero leaks + per-repo scopes pure',
    leaks === 0 && (perSt.json?.pulls || []).every(x => x.repo === 'wookat/speaktype')
      && (perTz.json?.pulls || []).every(x => x.repo === 'nghqqa/tizhou'), `leaks=${leaks}`);
  const mixed = q("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary5-tz-2' AND payload->>'repo' <> 'nghqqa/tizhou'");
  rec('O5', 'cross-repo isolation: receipts repo-bound per run (PG)', mixed === '0', `crossrows=${mixed}`);
}
// ── 4 boundary statuses ────────────────────────────────────────────────
{
  const u = await raw('/api/pulls');
  const f = await raw('/api/pulls?repo=x/y', { headers: H2 });
  const nf = await raw('/api/nope', { headers: H2 });
  const badJson = await fetch(BASE + '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: '{oops' });
  rec('O6', '401 / 403 / 404 / 400', u.status === 401 && f.status === 403 && nf.status === 404 && badJson.status === 400,
    `${u.status}/${f.status}/${nf.status}/${badJson.status}`);
  for (const c of ['mp-cc-nw', 'mp-cc-err']) { try { execSync(`docker rm -f ${c}`, { stdio: 'pipe' }); } catch {} }
  execSync('docker run -d --name mp-cc-nw --network mp-cc-net -p 48191:4730 '
    + '-e CONSOLE_PILOT_USER=pilot -e CONSOLE_PILOT_PASSWORD=pilot-read-only-2026 '
    + '-e CONSOLE_SESSION_SECRET=s7 -e CONSOLE_REPO_ALLOWLIST=wookat/speaktype,nghqqa/tizhou mp-canonical-console:candidate', { stdio: 'pipe' });
  execSync('docker run -d --name mp-cc-err --network mp-cc-net -p 48192:4730 '
    + '-e CONSOLE_PILOT_USER=pilot -e CONSOLE_PILOT_PASSWORD=pilot-read-only-2026 '
    + '-e CONSOLE_SESSION_SECRET=s8 -e CONSOLE_PG_DSN=postgresql://bad:bad@127.0.0.1:1/bad '
    + '-e CONSOLE_REPO_ALLOWLIST=wookat/speaktype,nghqqa/tizhou mp-canonical-console:candidate', { stdio: 'pipe' });
  await sleep(4000);
  const loginAt = async (port) => (await (await fetch(`http://127.0.0.1:${port}/api/auth/login`, { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(CREDS) })).headers.get('set-cookie') || '').split(';')[0];
  const nw = await (await fetch('http://127.0.0.1:48191/api/pulls', { headers: { cookie: await loginAt(48191) } })).json();
  const er = await (await fetch('http://127.0.0.1:48192/api/pulls', { headers: { cookie: await loginAt(48192) } })).json();
  rec('O7', 'BACKEND_NOT_WIRED (empty) · BACKEND_ERROR (detail)',
    nw.source === 'BACKEND_NOT_WIRED' && (nw.pulls || []).length === 0
      && er.source === 'BACKEND_ERROR' && !!er.error, `${nw.source}/${er.source}`);
  for (const c of ['mp-cc-nw', 'mp-cc-err']) { try { execSync(`docker rm -f ${c}`, { stdio: 'pipe' }); } catch {} }
}
// ── 5 invariants: stale / duplicate / CONFLICT / TTL ticket ────────────
{
  const stale = execSync('python -X utf8 D:/goai/mp-worktrees/console/verification/promotion/helpers/stale-check.py',
    { encoding: 'utf8', cwd: 'D:/goai/mp-worktrees/integration', stdio: ['ignore', 'pipe', 'pipe'], timeout: 120000 }).trim();
  rec('O8', 'stale head → gate REFUSE (never success)', stale.startsWith('REFUSE') && stale.includes('STALE'), stale);
  const st = q("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary5-st-426'");
  const tz = q("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary5-tz-2'");
  rec('O9', 'duplicate rows stable; divergent replay CONFLICT kept originals (recorded this round)',
    st === '2' && tz === '2', `st=${st} tz=${tz}`);
  const exp = q("SELECT count(*) FROM approval.tickets WHERE ticket_id='T-RC-EXPIRED' AND approval_expires_at < now()");
  rec('O10', 'TTL-expired ticket past-due in store (hidden from console by allowlist — by design)', exp === '1', `rows=${exp}`);
}
// ── TTL expiry + restart re-login ──────────────────────────────────────
{
  try { execSync('docker rm -f mp-cc-ttl', { stdio: 'pipe' }); } catch {}
  execSync('docker run -d --name mp-cc-ttl --network mp-cc-net -p 48193:4730 '
    + '-e CONSOLE_PILOT_USER=pilot -e CONSOLE_PILOT_PASSWORD=pilot-read-only-2026 '
    + '-e CONSOLE_SESSION_SECRET=s9 -e CONSOLE_SESSION_TTL_MS=3000 '
    + '-e CONSOLE_REPO_ALLOWLIST=wookat/speaktype,nghqqa/tizhou mp-canonical-console:candidate', { stdio: 'pipe' });
  await sleep(4000);
  let ttl = '';
  try {
    const lr = await fetch('http://127.0.0.1:48193/api/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify(CREDS) });
    const ck = (lr.headers.get('set-cookie') || '').split(';')[0];
    const now = await fetch('http://127.0.0.1:48193/api/pulls', { headers: { cookie: ck } });
    await sleep(4000);
    const later = await fetch('http://127.0.0.1:48193/api/pulls', { headers: { cookie: ck } });
    ttl = `${now.status}->${later.status}`;
  } finally { try { execSync('docker rm -f mp-cc-ttl', { stdio: 'pipe' }); } catch {} }
  execSync('docker restart mp-cc-console', { stdio: 'pipe' });
  let up = false;
  for (let i = 0; i < 20 && !up; i++) { await sleep(500); try { up = (await raw('/api/health')).status === 200; } catch {} }
  const dead = await raw('/api/pulls', { headers: H2 });
  const L3 = await login();
  const fresh = await raw('/api/pulls', { headers: { cookie: L3.cookie } });
  rec('O11', 'TTL expiry 200→401 (real knob) · restart: old 401 → re-login LIVE',
    ttl === '200->401' && up && dead.status === 401 && fresh.json?.source === 'POSTGRESQL_LIVE',
    `ttl=${ttl} dead=${dead.status} fresh=${fresh.json?.source}`);
}
// ── skipped items (recorded; NEVER counted as passed) ─────────────────
skip('S1', 'console_v3 只读 HTTP 联调 (live-v3.integration.test.mjs)', 'MERGEPILOT_V3_URL 未设置 — console_v3 服务不在本轮拓扑；snapshot 为正式数据源，不伪装联调已发生');

import { writeFileSync } from 'node:fs';
const failed = results.filter(r => !r.pass);
const summary = { total: results.length, pass: results.length - failed.length,
  fail: failed.length, skipped: skips.length };
writeFileSync(new URL('./ops-canonical-log.json', import.meta.url),
  JSON.stringify({ summary, results, skips, events, finished_at: new Date().toISOString() }, null, 1));
console.log(`\ncanonical ops: ${summary.pass}/${summary.total} PASS · ${summary.skipped} SKIPPED (recorded, not counted as passed)`);
if (failed.length) { console.log(failed.map(f => `FAIL ${f.id}: ${f.name}`).join('\n')); process.exit(1); }
