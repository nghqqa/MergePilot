// CONTROLLED_READONLY_USER_PILOT — approved internal user (pilot, sole
// approved operator; NO subjects added this round) over the CANONICAL console.
import { execSync } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const BASE = 'http://127.0.0.1:48190';
const CREDS = { user: 'pilot', password: 'pilot-read-only-2026' };
const results = [], skips = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
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

const L = await login();
const H = { cookie: L.cookie };

// ── U1 每用户只能看到授权仓库 ─────────────────────────────────────────
{
  const [p, pn, tk, ev] = await Promise.all([
    raw('/api/pulls', { headers: H }), raw('/api/pending', { headers: H }),
    raw('/api/tickets', { headers: H }), raw('/api/evidence', { headers: H }),
  ]);
  const allow = new Set(['wookat/speaktype', 'nghqqa/tizhou']);
  const visible = new Set([...(p.json?.pulls || []), ...(pn.json?.pending || []),
                           ...(tk.json?.tickets || [])].map(x => x.repo));
  // PG 侧确实存在未授权仓库行（fixture）——必须对用户不可见
  const pgRepos = q("SELECT DISTINCT payload->>'repo' FROM skill_receipt_outbox UNION SELECT DISTINCT repo_id FROM approval.tickets");
  const pgSet = new Set(pgRepos.split('\n').map(s => s.trim()).filter(Boolean));
  const hiddenExists = [...pgSet].some(r => !allow.has(r));
  const leaks = [...visible].filter(r => !allow.has(r));
  rec('U1', 'user sees ONLY authorized repos (unauthorized rows exist in PG but invisible)',
    leaks.length === 0 && hiddenExists && visible.size > 0,
    `visible=${[...visible].join(',')} hiddenInPg=${hiddenExists}`);
}
// ── U2 认证边界与恢复 ──────────────────────────────────────────────────
{
  const unauth = await raw('/api/pulls');
  const foreign = await raw('/api/pulls?repo=someone/else', { headers: H });
  const badpw = await raw('/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'nope' }) });
  const lo = await raw('/api/auth/logout', { method: 'POST', headers: { ...H, 'x-csrf-token': L.csrf } });
  const dead = await raw('/api/pulls', { headers: H });
  // TTL expiry (one-shot, real knob)
  try { execSync('docker rm -f mp-cc-ttl', { stdio: 'pipe' }); } catch {}
  execSync('docker run -d --name mp-cc-ttl --network mp-cc-net -p 48193:4730 '
    + '-e CONSOLE_PILOT_USER=pilot -e CONSOLE_PILOT_PASSWORD=pilot-read-only-2026 '
    + '-e CONSOLE_SESSION_SECRET=st -e CONSOLE_SESSION_TTL_MS=3000 '
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
  // restart recovery
  execSync('docker restart mp-cc-console', { stdio: 'pipe' });
  let up = false;
  for (let i = 0; i < 20 && !up; i++) { await sleep(500); try { up = (await raw('/api/health')).status === 200; } catch {} }
  const L2 = await login();
  const fresh = await raw('/api/pulls', { headers: { cookie: L2.cookie } });
  rec('U2', '401/403 · bad password · logout · TTL expiry · restart recovery',
    unauth.status === 401 && foreign.status === 403 && badpw.status === 401
      && lo.status === 200 && dead.status === 401 && ttl === '200->401'
      && up && fresh.json?.source === 'POSTGRESQL_LIVE',
    `${unauth.status}/${foreign.status}/${badpw.status}/${lo.status}/${dead.status} ttl=${ttl} fresh=${fresh.json?.source}`);
  H.cookie = L2.cookie; L.csrf = L2.csrf;
}
// ── U3 /core 数据一致性（API ↔ PG 交叉核对）───────────────────────────
{
  const [p, ev, au] = await Promise.all([
    raw('/api/pulls', { headers: H }), raw('/api/evidence', { headers: H }),
    raw('/api/audit', { headers: H }),
  ]);
  const tz = (p.json?.pulls || []).find(x => x.repo === 'nghqqa/tizhou');
  const st = (p.json?.pulls || []).find(x => x.repo === 'wookat/speaktype');
  const pgTzHead = q("SELECT payload->>'head_sha' FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary6-tz-2' LIMIT 1");
  const pgStHead = q("SELECT payload->>'head_sha' FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary6-st-426' LIMIT 1");
  const pgReceiptCount = q("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id' LIKE 'run-canary6%'");
  const apiReceiptCount = (ev.json?.evidence || []).filter(e => String(e.run_id || '').startsWith('run-canary6')).length;
  const pgGates = q("SELECT count(*) FROM skill_gate_audit WHERE run_id LIKE 'run-canary6%'");
  const apiGates = (au.json?.gate_decisions || []).filter(g => String(g.run_id || '').startsWith('run-canary6')).length;
  rec('U3', 'API ↔ PG consistency: heads exact, receipts & gate counts match',
    tz?.head_sha === pgTzHead && st?.head_sha === pgStHead
      && tz?.pr_number === 2 && st?.pr_number === 426
      && Number(pgReceiptCount) === apiReceiptCount && Number(pgGates) === apiGates,
    `heads=${tz?.head_sha?.slice(0, 8)}/${st?.head_sha?.slice(0, 8)} receipts=${apiReceiptCount}/${pgReceiptCount} gates=${apiGates}/${pgGates}`);
}
// ── U4 fail-closed 不变式 ──────────────────────────────────────────────
{
  const stale = execSync('python -X utf8 D:/goai/mp-worktrees/console/verification/promotion/helpers/stale-check.py',
    { encoding: 'utf8', cwd: 'D:/goai/mp-worktrees/integration', stdio: ['ignore', 'pipe', 'pipe'], timeout: 120000 }).trim();
  const dupSt = q("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary6-st-426'");
  const dupTz = q("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary6-tz-2'");
  const expTicket = q("SELECT count(*) FROM approval.tickets WHERE ticket_id='T-RC-EXPIRED' AND approval_expires_at < now()");
  const tk = await raw('/api/tickets', { headers: H });
  const expiredHidden = !(tk.json?.tickets || []).some(t => t.ticket_id === 'T-RC-EXPIRED');
  rec('U4', 'fail-closed: stale REFUSE · duplicate stable · CONFLICT kept originals · expired ticket past-due + invisible',
    stale.startsWith('REFUSE') && stale.includes('STALE') && dupSt === '2' && dupTz === '2'
      && expTicket === '1' && expiredHidden,
    `${stale} rows=${dupSt}/${dupTz} expTicket=${expTicket} hidden=${expiredHidden}`);
}
// ── U5/U6 由浏览器会话与 MinIO/rollback 脚本覆盖（单独执行记录）───────
skips.push({ id: '-', name: 'U5/U6 in companion steps', reason: 'browser + MinIO + rollback executed as separate scripted steps this round; results recorded in report' });

import { writeFileSync } from 'node:fs';
const failed = results.filter(r => !r.pass);
writeFileSync(new URL('./user-pilot-log.json', import.meta.url),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length, fail: failed.length },
    results, skips, finished_at: new Date().toISOString() }, null, 1));
console.log(`\nuser pilot: ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
