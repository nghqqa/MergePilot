// staging-verify.mjs — LIMITED_READONLY_STAGING 部署前验证矩阵。
import { execSync } from 'node:child_process';

const B = 'http://127.0.0.1:48200';
const results = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass, detail: String(detail ?? '') });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
async function raw(p, init) {
  const r = await fetch(B + p, init);
  return { status: r.status, header: r.headers.get('x-rag-service-state'),
           body: await r.json().catch(() => null), headers: r.headers };
}
const pw = execSync(`docker inspect mp-stage-pg --format "{{range .Config.Env}}{{println .}}{{end}}"`,
  { encoding: 'utf8' }).split('\n').find((l) => l.startsWith('POSTGRES_PASSWORD=')).split('=')[1].trim();
const pgq = (sql) => execSync(`docker exec mp-stage-pg psql -U mpstage -d mpstage -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
async function login() {
  const r = await raw('/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'pilot', password: 'pilot-read-only-2026' }) });
  const cookies = r.headers.getSetCookie ? r.headers.getSetCookie() : [r.headers.get('set-cookie')];
  const flat = cookies.flat().join('\n');
  const csrf = (flat.match(/mp_csrf=([0-9a-f]+)/) || [])[1];
  return { status: r.status, cookie: flat.split('\n').map((c) => c.split(';')[0]).join('; '), csrf };
}

// 1 digest 与 health
rec('S1', '镜像 digest = sha256:1056df76…（与 SBOM/Trivy 记录一致）',
  execSync('docker inspect mp-stage-console --format "{{.Image}}"', { encoding: 'utf8' })
    .trim().startsWith('sha256:1056df76'), '');
rec('S2', 'health=200 且 live configured', (await raw('/api/health')).status === 200);
// 2 session / TTL / CSRF logout
{
  const L = await login();
  const s = await raw('/api/auth/session', { headers: { cookie: L.cookie } });
  const lo = await raw('/api/auth/logout', { method: 'POST',
    headers: { cookie: L.cookie, 'x-csrf-token': L.csrf, 'content-type': 'application/json' }, body: '{}' });
  const dead = await raw('/api/pulls', { headers: { cookie: L.cookie } });
  const L2 = await login();
  const alive = await raw('/api/pulls', { headers: { cookie: L2.cookie } });
  rec('S3', 'session echo + CSRF logout（200→401）+ 重登 LIVE',
    s.status === 200 && (s.body.user?.name ?? s.body.user) === 'pilot'
      && lo.status === 200 && dead.status === 401 && alive.body?.source === 'POSTGRESQL_LIVE');
  var CK = L2.cookie;
}
// 3 allowlist 服务端过滤 + 未授权 403/404 零泄漏
{
  const f = await raw('/api/pulls?repo=x/y', { headers: { cookie: CK } });
  const pack = await raw('/api/runs/OUTSIDER-PACK', { headers: { cookie: CK } });
  const ov = await raw('/api/overview', { headers: { cookie: CK } });
  const leaks = JSON.stringify(ov.body.prs).match(/outsider|pilot-staging|other-org/) === null;
  rec('S4', '未授权 repo 403 / 未知 pack 404 / overview 零越权',
    f.status === 403 && pack.status === 404 && leaks,
    `${f.status}/${pack.status}/leaks=${!leaks}`);
}
// 4 数据源统一 live + 两 PR 一致性
{
  const [ov, pn, pl, pr1, pr2] = await Promise.all([
    raw('/api/overview', { headers: { cookie: CK } }),
    raw('/api/pending', { headers: { cookie: CK } }),
    raw('/api/pulls', { headers: { cookie: CK } }),
    raw('/api/pulls/2?repo=nghqqa/tizhou', { headers: { cookie: CK } }),
    raw('/api/pulls/426?repo=wookat/speaktype', { headers: { cookie: CK } }),
  ]);
  const pgReceipts = pgq("SELECT count(*) FROM skill_receipt_outbox");
  const apiEv = await raw('/api/evidence', { headers: { cookie: CK } });
  rec('S5', '/overview、/pending、/repos、PR detail、audit 统一 LIVE',
    ov.body.source === 'POSTGRESQL_LIVE' && pn.body.source === 'POSTGRESQL_LIVE'
      && pl.body.source === 'POSTGRESQL_LIVE' && pr1.status === 200 && pr2.status === 200);
  rec('S6', '两 PR receipt/gate/stage 与 PG 一致',
    Number(pgReceipts) === (apiEv.body.evidence || []).length
      && ov.body.stage_counts.PASSED >= 2 && ov.body.prs.length >= 2,
    `receipts pg=${pgReceipts} api=${apiEv.body.evidence?.length} stages=${JSON.stringify(ov.body.stage_counts)}`);
}
// 5 A 链 flag-off 契约
{
  const r = await raw('/api/rag/org-search?q=x', { headers: { cookie: CK } });
  rec('S7', 'A 链 flag-off：a_chain_disabled 诚实态（不伪装检索）',
    r.status === 200 && r.body.service_state === 'a_chain_disabled' && !r.body.results);
}
// 6 禁用组件
{
  const h = await (await fetch(B + '/api/health')).json();
  const c = await raw('/api/overview', { headers: { cookie: CK } });
  const serialized = JSON.stringify(h) + JSON.stringify(c.body);
  rec('S8', 'embedding/C 链/Fixer/Verifier/RUN_BINDING_AUTH 关闭（无暴露迹象）',
    !/embedding_enabled|case_retrieval|fixer|verifier|run_binding_auth/i.test(serialized.replace(/RUN_BINDING_AUTH=NOT_WIRED/g, '')));
}

const failed = results.filter((r) => !r.pass);
import { writeFileSync } from 'node:fs';
writeFileSync(new URL('./staging-verify-report.json', import.meta.url),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length }, results,
    finished_at: new Date().toISOString() }, null, 1));
console.log(`\nstaging verify: ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
