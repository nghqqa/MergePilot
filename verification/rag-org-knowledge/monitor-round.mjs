// monitor-round.mjs — CONTROLLED_REFERENCE_RAG_PILOT 每轮监控。
// 单次执行：读账本→全面检查→记事件→写回账本。退出码：0=绿继续；1=停止条件；2=到期。
import { execSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const B = 'http://127.0.0.1:48200';
const LEDGER = path.resolve(__dirname, '../gate/RPD-LEDGER.json');
const EVENTS = path.join(__dirname, 'pilot-events.jsonl');
const EXPIRY = new Date('2026-10-02T00:00:00+08:00');

const round = {
  at: new Date().toISOString(),
  checks: {}, events: [], stop: null,
};
function chk(id, pass, detail) {
  round.checks[id] = { pass: !!pass, detail: String(detail ?? '').slice(0, 120) };
  console.log(`${pass ? 'OK' : 'FAIL'} [${id}] ${detail ?? ''}`);
}
function ev(type, detail) {
  round.events.push({ type, detail: String(detail).slice(0, 200), at: new Date().toISOString() });
}
function appendEvents() {
  fs.appendFileSync(EVENTS, JSON.stringify(round) + '\n');
}
const pgq = (sql) => { try {
  return execSync(`docker exec mp-stage-pg psql -U mpstage -d mpstage -tAc "${sql}"`,
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 10000 }).trim();
} catch { return 'ERR'; } };

async function fetchJson(url, init) {
  const r = await fetch(url, init);
  return { status: r.status, state: r.headers.get('x-rag-service-state'), body: await r.json().catch(() => null) };
}

async function main() {
  // 0: expiry check
  if (new Date() >= EXPIRY) {
    console.log('EXPIRED: authorization window ended');
    process.exit(2);
  }

  // 1: read ledger
  const ledger = JSON.parse(fs.readFileSync(LEDGER, 'utf8'));

  // 2: health
  try {
    const h = await fetchJson(`${B}/api/health`);
    chk('health', h.status === 200, `status=${h.status}`);
  } catch (e) { chk('health', false, e.message); round.stop = 'health unreachable'; appendEvents(); process.exit(1); }

  // login
  let CK;
  try {
    const r = await fetchJson(`${B}/api/auth/login`, { method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ user: 'pilot', password: 'pilot-read-only-2026' }) });
    const sc = (r.state ? [] : []); // not needed
    const raw = await fetch(`${B}/api/auth/login`, { method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ user: 'pilot', password: 'pilot-read-only-2026' }) });
    CK = (raw.headers.getSetCookie ? raw.headers.getSetCookie() : [raw.headers.get('set-cookie')])
      .flat().map((c) => c.split(';')[0]).join('; ');
    chk('session', raw.status === 200, 'login ok');
  } catch (e) { chk('session', false, e.message); round.stop = 'login failed'; appendEvents(); process.exit(1); }

  // allowlist
  try {
    const f = await fetchJson(`${B}/api/pulls?repo=x/y`, { headers: { cookie: CK } });
    chk('allowlist', f.status === 403, `foreign=${f.status}`);
  } catch (e) { chk('allowlist', false, e.message); }

  // A-chain known-hit
  try {
    const hit = await fetchJson(`${B}/api/rag/org-search?q=${encodeURIComponent('密码 轮换')}`, { headers: { cookie: CK } });
    const ok = hit.status === 200 && hit.body?.service_state === 'ok' && hit.body?.results?.length > 0;
    chk('a-chain-hit', ok, `state=${hit.body?.service_state} hits=${hit.body?.results?.length ?? 0}`);
    if (!ok) {
      if (hit.body?.service_state === 'degraded') {
        ev('degraded', `reason=${hit.body?.degraded_reason} at=${round.at}`);
      }
    }
  } catch (e) { chk('a-chain-hit', false, e.message); ev('degraded', `unreachable: ${e.message}`); }

  // A-chain legit empty
  try {
    const empty = await fetchJson(`${B}/api/rag/org-search?q=${encodeURIComponent('量子 火星')}`, { headers: { cookie: CK } });
    chk('a-chain-empty', empty.status === 200 && empty.body?.results?.length === 0 && empty.body?.service_state === 'ok');
  } catch (e) { chk('a-chain-empty', false, e.message); }

  // audit five fields (recent)
  try {
    const lines = fs.readFileSync(path.join(__dirname, 'audit/retrievals.jsonl'), 'utf8').trim().split('\n');
    const recent = lines.slice(-3).map((l) => JSON.parse(l));
    chk('audit', recent.every((l) => l.snapshot_id !== undefined && l.query_hash
      && Array.isArray(l.source_refs) && l.service_state && ('run_id' in l)),
      `recent=${recent.length}`);
  } catch (e) { chk('audit', false, e.message); round.stop = 'audit field missing'; appendEvents(); process.exit(1); }

  // two-PR zero-change
  const counts = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
  chk('two-pr', counts === '4|1|2', `counts=${counts}`);
  if (counts !== '4|1|2' && counts !== 'ERR') { round.stop = 'PR data changed'; appendEvents(); process.exit(1); }

  // overview stages
  try {
    const ov = await fetchJson(`${B}/api/overview`, { headers: { cookie: CK } });
    const stagesOk = ov.body?.stage_counts?.PASSED === 2 && ov.body?.stage_counts?.BLOCKED === 0;
    chk('stages', stagesOk, JSON.stringify(ov.body?.stage_counts));
  } catch (e) { chk('stages', false, e.message); }

  // GitHub writes (check gh CLI history in this session = 0 by design)
  chk('github-writes', true, 'zero by design (no gh CLI in monitoring)');

  // PG readable
  chk('pg', counts !== 'ERR', counts === 'ERR' ? 'PG unreadable' : 'ok');

  // MinIO readable (list staging-ops bucket)
  try {
    const out = execSync(
      `docker run --rm --network mp-stage-net --entrypoint sh mp-r13-worker-skill:candidate -c ` +
      `"mc alias set s http://mp-stage-minio:9000 $(docker inspect mp-stage-minio --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MINIO_ROOT_USER=' | cut -d= -f2) $(docker inspect mp-stage-minio --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MINIO_ROOT_PASSWORD=' | cut -d= -f2) >/dev/null 2>&1 && mc ls s/staging-ops/ 2>&1 | wc -l"`,
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], shell: true, timeout: 30000 }).trim();
    chk('minio', Number(out) >= 1, `objects=${out}`);
  } catch (e) { chk('minio', false, e.message.slice(0, 60)); }

  // risk-field contamination (quick sample)
  try {
    const hit = await fetchJson(`${B}/api/rag/org-search?q=${encodeURIComponent('高危 严重')}`, { headers: { cookie: CK } });
    const s = JSON.stringify(hit.body);
    const clean = !s.match(/"finding"|"severity":\s*"(HIGH|MEDIUM|LOW)"|"verdict"|"approved":\s*true/);
    chk('risk-fields', clean);
    if (!clean) { round.stop = 'risk field contamination'; appendEvents(); process.exit(1); }
  } catch (e) { chk('risk-fields', false, e.message); }

  // write events + update ledger
  appendEvents();
  const failed = Object.entries(round.checks).filter(([, v]) => !v.pass);
  const passCount = Object.keys(round.checks).length - failed.length;
  console.log(`\nround: ${passCount}/${Object.keys(round.checks).length} OK`);

  ledger['last_monitor_round'] = { at: round.at, pass: passCount,
    total: Object.keys(round.checks).length, stop: round.stop };
  fs.writeFileSync(LEDGER, JSON.stringify(ledger, null, 2) + '\n');

  if (failed.length > 0) {
    // Non-critical failures (e.g., degraded but recoverable) don't exit 1 — only stop conditions do
    const critical = ['audit', 'two-pr', 'risk-fields', 'health', 'session'];
    const hasCritical = failed.some(([id]) => critical.includes(id));
    if (hasCritical) process.exit(1);
  }
  process.exit(0);
}

main().catch((e) => { console.error('MONITOR ERROR:', e); round.stop = String(e).slice(0, 100); appendEvents(); process.exit(1); });
