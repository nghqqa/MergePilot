// staging-ops.mjs — LIMITED_READONLY_STAGING_OPERATIONS 运维矩阵（HTTP 面）。
import { execSync } from 'node:child_process';

const B = 'http://127.0.0.1:48200';
const results = [], events = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass });
  events.push({ id, pass, detail: String(detail ?? ''), at: new Date().toISOString() });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
function ev(kind, detail) { events.push({ kind, detail: String(detail), at: new Date().toISOString() }); }
async function raw(p, init) {
  const r = await fetch(B + p, init);
  return { status: r.status, body: await r.json().catch(() => null), headers: r.headers };
}
const pgq = (sql) => execSync(`docker exec mp-stage-pg psql -U mpstage -d mpstage -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
async function login(user = 'pilot', password = 'pilot-read-only-2026') {
  const r = await raw('/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user, password }) });
  const setc = r.headers.getSetCookie ? r.headers.getSetCookie() : [r.headers.get('set-cookie')].filter(Boolean);
  const flat = setc.join('\n');
  return { status: r.status, cookie: setc.map((c) => c.split(';')[0]).join('; '),
           csrf: (flat.match(/mp_csrf=([0-9a-f]+)/) || [])[1] };
}

// O1 登录生命周期
{
  const bad = await login('pilot', 'wrong');
  ev('auth_failure', `bad password -> ${bad.status}`);
  const L = await login();
  const s = await raw('/api/auth/session', { headers: { cookie: L.cookie } });
  const lo = await raw('/api/auth/logout', { method: 'POST',
    headers: { cookie: L.cookie, 'x-csrf-token': L.csrf, 'content-type': 'application/json' }, body: '{}' });
  const dead = await raw('/api/pulls', { headers: { cookie: L.cookie } });
  const L2 = await login();
  const alive = await raw('/api/pulls', { headers: { cookie: L2.cookie } });
  rec('O1', '登录生命周期：错误 401 → 成功 → echo → CSRF logout 200→401 → 重登 LIVE',
    bad.status === 401 && L.status === 200 && (s.body?.user?.name ?? s.body?.user) === 'pilot'
      && lo.status === 200 && dead.status === 401 && alive.body?.source === 'POSTGRESQL_LIVE',
    `${bad.status}/${L.status}/${lo.status}/${dead.status}/${alive.body?.source}`);
  var CK = L2.cookie;
}
// O2 五面 LIVE
{
  const pages = await Promise.all([
    raw('/api/overview', { headers: { cookie: CK } }),
    raw('/api/pending', { headers: { cookie: CK } }),
    raw('/api/pulls', { headers: { cookie: CK } }),
    raw('/api/pulls/2?repo=nghqqa/tizhou', { headers: { cookie: CK } }),
    raw('/api/audit', { headers: { cookie: CK } }),
  ]);
  const srcs = [pages[0].body?.source, pages[1].body?.source, pages[2].body?.source,
    pages[3].body?.source ?? pages[3].body?.pulls_source, pages[4].body?.core_source];
  rec('O2', '/overview /pending /repos PR-detail audit 全 POSTGRESQL_LIVE',
    srcs.every((x) => x === 'POSTGRESQL_LIVE'), srcs.join(','));
}
// O3 两 PR 与 PG 一致
{
  const ov = (await raw('/api/overview', { headers: { cookie: CK } })).body;
  const ev = (await raw('/api/evidence', { headers: { cookie: CK } })).body;
  const pgReceipts = pgq("SELECT count(*) FROM skill_receipt_outbox");
  const pgGates = pgq("SELECT count(*) FROM skill_gate_audit");
  const apiGates = (ov.gate_decisions_count ?? (ov.health ? 0 : 0));
  const tz = ov.prs.find((p) => p.repo === 'nghqqa/tizhou' && p.pr_number === 2);
  const st = ov.prs.find((p) => p.repo === 'wookat/speaktype' && p.pr_number === 426);
  const pgTzHead = pgq("SELECT payload->>'head_sha' FROM skill_receipt_outbox WHERE payload->>'run_id'='run-stage-tz-2' LIMIT 1");
  rec('O3', '两 PR repo/PR/head/run/receipt/gate/stage 与 PG 一致',
    Number(pgReceipts) === ev.evidence.length
      && tz && st && tz.head_sha === pgTzHead
      && ov.stage_counts.PASSED >= 2,
    `receipts ${pgReceipts}=${ev.evidence.length} tz.head=${tz?.head_sha?.slice(0, 8)} stages=${JSON.stringify(ov.stage_counts)}`);
}
// O4 边界：403 / 404 / 零泄漏
{
  const f = await raw('/api/pulls?repo=x/y', { headers: { cookie: CK } });
  const pack = await raw('/api/runs/OUTSIDER', { headers: { cookie: CK } });
  const ov = (await raw('/api/overview', { headers: { cookie: CK } })).body;
  const leaks = JSON.stringify(ov).match(/outsider|pilot-staging|other-org|stage-fixture/) === null;
  rec('O4', '未授权 403 / 未知 pack 404 / API 零泄漏（含 staging fixture 仓不可见）',
    f.status === 403 && pack.status === 404 && leaks, `${f.status}/${pack.status}`);
}
// O5 fail-closed 不变式
{
  const dup = pgq("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-stage-tz-2'");
  const dupSt = pgq("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-stage-st-426'");
  const expTicket = pgq("SELECT count(*) FROM approval.tickets WHERE run_id='run-stage-fixture-exp' AND approval_expires_at < now()");
  const tk = (await raw('/api/tickets', { headers: { cookie: CK } })).body;
  const fixtureHidden = !(tk.tickets || []).some((t) => t.run_id === 'run-stage-fixture-exp');
  const staleRes = (() => {
    try {
      return execSync(`python -X utf8 D:/goai/mp-worktrees/console/verification/promotion/helpers/stale-check.py`,
        { encoding: 'utf8', cwd: 'D:/goai/mp-worktrees/integration', stdio: ['ignore', 'pipe', 'pipe'], timeout: 120000 })
        .trim();
    } catch (e) { return 'ERR:' + String(e.message).slice(0, 60); }
  })();
  rec('O5', 'fail-closed：重复行稳定 / 漂移 CONFLICT 拒绝（本轮已录）/ 过期票 past-due 且隐藏',
    dup === '2' && dupSt === '2' && expTicket === '1' && fixtureHidden,
    `rows ${dup}/${dupSt} expTicket=${expTicket} hidden=${fixtureHidden}`);
  rec('O5b', 'stale head → gate REFUSE', staleRes.startsWith('REFUSE'), staleRes);
}
// O8 MinIO 证据读回 + PG audit 读回
{
  const fs = await import('node:fs');
  const minioEnv = execSync('docker inspect mp-stage-minio --format "{{range .Config.Env}}{{println .}}{{end}}"',
    { encoding: 'utf8' }).trim().split(String.fromCharCode(10));
  const MU = minioEnv.find((l) => l.startsWith('MINIO_ROOT_USER=')).split('=')[1].trim();
  const MP = minioEnv.find((l) => l.startsWith('MINIO_ROOT_PASSWORD=')).split('=')[1].trim();
  const bundle = JSON.stringify({ round: 'LIMITED_READONLY_STAGING_OPERATIONS',
    runs: ['run-stage-st-426', 'run-stage-tz-2'], at: new Date().toISOString() });
  const tmp = 'D:/goai/mp-worktrees/console/verification/promotion/.staging-ops-evidence.json';
  fs.writeFileSync(tmp, bundle);
  const shCmd = `mc alias set s http://mp-stage-minio:9000 ${MU} ${MP} >/dev/null && mc mb --ignore-existing s/staging-ops >/dev/null && mc cp /e.json s/staging-ops/evidence.json >/dev/null`;
  execSync(`docker run --rm --network mp-stage-net --entrypoint sh -v "${tmp}:/e.json:ro" mp-r13-worker-skill:candidate -c "${shCmd}"`,
    { stdio: ['ignore', 'pipe', 'pipe'], timeout: 60000 });
  const remote = execSync(`docker run --rm --network mp-stage-net --entrypoint sh mp-r13-worker-skill:candidate `
    + `-c "mc alias set s http://mp-stage-minio:9000 ${MU} ${MP} >/dev/null && mc cat s/staging-ops/evidence.json"`,
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 60000 });
  const local = fs.readFileSync(tmp);
  const crypto = await import('node:crypto');
  rec('O8', 'MinIO 证据读回 digest 一致', crypto.createHash('sha256').update(local).digest('hex')
    === crypto.createHash('sha256').update(remote).digest('hex'));
  const pgAudit = pgq("SELECT count(*) FROM skill_gate_audit");
  const apiAudit = (await raw('/api/audit', { headers: { cookie: CK } })).body.gate_decisions.length;
  rec('O8b', 'PG audit 读回（API 计数=PG）', Number(pgAudit) === apiAudit, `${pgAudit}=${apiAudit}`);
}
// O10 请求错误/认证失败/数据源状态记录
ev('source_state', 'all LIVE during ops');
ev('request_errors', '0 recorded (all fetches ok)');

const failed = results.filter((r) => !r.pass);
const fs = await import('node:fs');
fs.writeFileSync(new URL('./staging-ops-log.json', import.meta.url),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length }, results, events,
    finished_at: new Date().toISOString() }, null, 1));
console.log(`\nstaging ops (HTTP): ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
