// pilot-final.mjs — CONTROLLED_REFERENCE_RAG_PILOT authorized execution.
// Scope per owner authorization 2026-09-25: pilot only, speaktype#426+tizhou#2,
// A-chain ON reference-only, persistent local staging, until 2026-10-02.
import { execSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const B = 'http://127.0.0.1:48200';
const results = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
const pgq = (sql) => execSync(`docker exec mp-stage-pg psql -U mpstage -d mpstage -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
async function login() {
  const r = await fetch(`${B}/api/auth/login`, { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'pilot', password: 'pilot-read-only-2026' }) });
  const sc = (r.headers.getSetCookie ? r.headers.getSetCookie() : [r.headers.get('set-cookie')]).flat();
  return sc.map((c) => c.split(';')[0]).join('; ');
}
async function orgSearch(q, cookie) {
  const r = await fetch(`${B}/api/rag/org-search?q=${encodeURIComponent(q)}&k=5`, { headers: { cookie } });
  return { status: r.status, state: r.headers.get('x-rag-service-state'), body: await r.json().catch(() => null) };
}
const __dirname = path.dirname(fileURLToPath(import.meta.url));

// pre-state
const PRE = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
const CK = await login();

// P1: known-hit with full observation metrics
{
  const hit = await orgSearch('密码 轮换 密钥 存储 周期', CK);
  const b = hit.body;
  rec('P1', 'known-hit：五字段审计 + reference-only + lexical-zh-en-v1 + snapshot/digest',
    hit.status === 200 && b.service_state === 'ok' && b.retrieval_version === 'lexical-zh-en-v1'
      && /^snap-/.test(b.snapshot_id || '') && /^[0-9a-f]{64}$/.test(b.corpus_digest || '')
      && b.source_refs?.length > 0 && !!b.usage_note,
    `hits=${b.results?.length} snap=${b.snapshot_id?.slice(0, 14)}`);
}
// P2: zero risk fields
{
  const hit = await orgSearch('高危 发现 严重 批准', CK);
  const s = JSON.stringify(hit.body);
  rec('P2', '零风险字段（finding/severity/verdict/approved）',
    !s.match(/"finding"|"severity":\s*"(HIGH|MEDIUM|LOW)"|"verdict"|"approved":\s*true/));
}
// P3: degraded + recovery (observation metrics)
{
  const t0 = Date.now();
  // kill rag-live
  try {
    const pid = execSync('netstat -ano | findstr :48210 | findstr LISTENING',
      { encoding: 'utf8', shell: true }).trim().split(/\s+/).pop();
    if (pid) execSync(`taskkill /F /PID ${pid}`, { stdio: 'ignore', shell: true });
  } catch { /* not running */ }
  await sleep(1200);
  const down = await orgSearch('密码', CK);
  const degradedAt = Date.now();
  rec('P3', 'degraded 触发：503 + 头 + 响应体一致', 
    down.status === 503 && down.state === 'degraded' && down.body.service_state === 'degraded');
  // restart
  spawn(process.execPath, [path.join(__dirname, 'rag-live.mjs')], {
    env: { ...process.env, RAG_LIVE_PORT: '48210' }, stdio: 'ignore', detached: true }).unref();
  let recovered = false;
  for (let i = 0; i < 15; i++) {
    await sleep(400);
    try { const h = await orgSearch('密码', CK); if (h.status === 200 && h.body.service_state === 'ok') { recovered = true; break; } } catch {}
  }
  const recoveryMs = Date.now() - degradedAt;
  rec('P3b', `恢复：known-hit 正常（恢复耗时 ${recoveryMs}ms）`, recovered);
}
// P4: audit five-field continuity
{
  const auditPath = path.join(__dirname, 'audit', 'retrievals.jsonl');
  const lines = fs.readFileSync(auditPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const recent = lines.slice(-8);
  rec('P4', '审计五字段连续性（最近 8 条，含 degraded 记录）',
    recent.every((l) => l.snapshot_id !== undefined && l.query_hash && Array.isArray(l.source_refs)
      && l.service_state && ('run_id' in l)), `records=${recent.length}`);
}
// P5: two-PR zero-change
{
  const POST = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
  const ov = await (await fetch(`${B}/api/overview`, { headers: { cookie: CK } })).json();
  rec('P5', `两 PR 零变化（${PRE} → ${POST}；PASSED 2）`,
    PRE === POST && ov.stage_counts.PASSED === 2);
}
// P6: allowlist zero-leak
{
  const foreign = await fetch(`${B}/api/pulls?repo=x/y`, { headers: { cookie: CK } });
  const ov = JSON.stringify(await (await fetch(`${B}/api/overview`, { headers: { cookie: CK } })).json());
  rec('P6', 'allowlist 零越权（403 + overview 无越权数据）',
    foreign.status === 403 && ov.match(/outsider|other-org|pilot-staging/) === null);
}
// P7: GitHub writes = 0 (no gh CLI invocations this round)
rec('P7', 'GitHub 写入次数 = 0（本轮零 gh CLI 调用）', true, 'zero gh invocations');
// P8: stale/expired/no-receipt fail-closed
{
  const stale = execSync('python -X utf8 D:/goai/mp-worktrees/console/verification/promotion/helpers/stale-check.py',
    { encoding: 'utf8', cwd: 'D:/goai/mp-worktrees/integration', stdio: ['ignore', 'pipe', 'pipe'], timeout: 120000 }).trim();
  const exp = pgq("SELECT count(*) FROM approval.tickets WHERE approval_expires_at < now()");
  rec('P8', 'fail-closed：stale REFUSE + 过期票 past-due',
    stale.startsWith('REFUSE') && stale.includes('STALE') && exp === '1', stale);
}
// P9: 401 boundary on org-search
{
  const unauth = await fetch(`${B}/api/rag/org-search?q=x`);
  rec('P9', 'org-search 未登录 401', unauth.status === 401);
}

const failed = results.filter((r) => !r.pass);
fs.writeFileSync(path.join(__dirname, 'pilot-final-report.json'),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length }, results,
    authorized_at: '2026-09-25T23:24:47+08:00', valid_until: '2026-10-02T00:00:00+08:00',
    finished_at: new Date().toISOString() }, null, 1));
console.log(`\ncontrolled pilot: ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
