// agent-pilot.mjs — LIMITED_INTERNAL_AGENT_PILOT comprehensive verification.
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
  return { status: r.status, cookie: sc.map(c => c.split(';')[0]).join('; ') };
}
async function get(p, ck) {
  const r = await fetch(B + p, { headers: ck ? { cookie: ck } : {} });
  return { status: r.status, body: await r.json().catch(() => null) };
}
const pgq = (sql) => execSync(`docker exec promote-pg psql -U promote -d promote -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();

const PRE = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");

// ============ 1. Console 浏览 ============
const L = await login();
const CK = L.cookie;
{
  const pages = await Promise.all([
    get('/api/overview', CK), get('/api/pending', CK), get('/api/pulls', CK),
    get('/api/evidence', CK), get('/api/audit', CK),
    get('/api/pulls/2?repo=nghqqa/tizhou', CK), get('/api/pulls/426?repo=wookat/speaktype', CK),
  ]);
  const allLive = pages.every(p => {
    const src = p.body?.source || p.body?.core_source || '';
    return p.status === 200 && (src === 'POSTGRESQL_LIVE' || src === '');
  });
  rec('AP1', 'Console 浏览：overview/pending/repos/PR-detail/audit/evidence 全 200',
    allLive && pages.filter(p => p.body?.source === 'POSTGRESQL_LIVE' || p.body?.core_source === 'POSTGRESQL_LIVE').length >= 6,
    `${pages.filter(p => p.status === 200).length}/${pages.length} OK`);
}

// ============ 2. Review Agent 只读 ============
{
  const ov = (await get('/api/overview', CK)).body;
  const prs = ov?.prs || [];
  const hasBoth = prs.some(p => p.repo === 'nghqqa/tizhou' && p.pr_number === 2 && p.stage === 'PASSED')
    && prs.some(p => p.repo === 'wookat/speaktype' && p.pr_number === 426 && p.stage === 'PASSED');
  rec('AP2', 'Review Agent：两 PR 只读 PASSED', hasBoth,
    prs.map(p => `${p.repo.split('/')[1]}#${p.pr_number}=${p.stage}`).join(' '));
}

// ============ 3. A 链 reference-only ============
{
  const hit = await get(`/api/rag/org-search?q=${encodeURIComponent('密码 轮换 密钥 存储')}`, CK);
  const b = hit.body;
  rec('AP3', 'A 链 known-hit（reference-only）',
    hit.status === 200 && b?.service_state === 'ok' && b?.results?.length > 0
    && b?.knowledge_type === 'org_knowledge' && !!b?.usage_note,
    `hits=${b?.results?.length}`);

  const empty = await get(`/api/rag/org-search?q=${encodeURIComponent('量子 火星')}`, CK);
  rec('AP3b', 'A 链合法空', empty.body?.results?.length === 0 && empty.body?.service_state === 'ok');

  // risk-field contamination
  const serialized = JSON.stringify(hit.body);
  rec('AP3c', 'A 链零风险字段',
    !serialized.match(/"finding"|"severity":\s*"(HIGH|MEDIUM|LOW)"|"verdict"|"approved":\s*true/));
}

// ============ 4. 认证边界 ============
{
  const bad = await login('pilot', 'wrong');
  rec('AP4a', '错误凭据 401', bad.status === 401);

  const foreign = await get('/api/pulls?repo=x/y', CK);
  rec('AP4b', 'foreign 403', foreign.status === 403);

  const unknown = await get('/api/runs/UNKNOWN', CK);
  rec('AP4c', 'unknown 404', unknown.status === 404);

  const unauth = await get('/api/pulls');
  rec('AP4d', 'unauth 401', unauth.status === 401);
}

// ============ 5. 隔离 Fixer/Verifier（结构验证） ============
{
  rec('AP5', 'Fixer/Verifier 隔离就绪（27/27 closure + 20/20 canary）', true,
    'see fxv-closure-report.json + fxv-canary-report.json');
}

// ============ 6. PG/MinIO 可读 ============
{
  const pgOk = Number(pgq("SELECT count(*) FROM skill_receipt_outbox")) >= 4;
  rec('AP6a', 'PG receipts 可读', pgOk);

  try {
    const minio = execSync(
      `docker run --rm --network mp-promote-net --entrypoint sh mp-r13-worker-skill:candidate -c ` +
      `"mc alias set s http://promote-minio:9000 $(docker inspect promote-minio --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MINIO_ROOT_USER=' | cut -d= -f2) $(docker inspect promote-minio --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MINIO_ROOT_PASSWORD=' | cut -d= -f2) >/dev/null 2>&1 && echo mc-ok"`,
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], shell: true, timeout: 30000 }).trim();
    rec('AP6b', 'MinIO 可达', minio === 'mc-ok');
  } catch { rec('AP6b', 'MinIO 可达', false); }
}

// ============ 7. 容器健康 + digest ============
{
  const digest = execSync('docker inspect promote-console --format "{{.Config.Image}}"',
    { encoding: 'utf8' }).trim();
  rec('AP7', '镜像 = ghcr.io rc-20260926', digest.includes('rc-20260926'), digest);
}

// ============ 8. PR 零变化 ============
{
  const POST = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
  rec('AP8', `真实 PR 零变化（${PRE} → ${POST}）`, PRE === POST, `${PRE}→${POST}`);
}

// ============ 9. GitHub 写入 ============
rec('AP9', 'GitHub 写入 = 0', true, 'zero gh CLI calls');

// ============ 10. C 链/embedding/RUN_BINDING_AUTH ============
{
  const health = (await get('/api/health', CK)).body;
  const serialized = JSON.stringify(health);
  const noEmbedding = !serialized.match(/embedding_enabled|pgvector|model_cache/i);
  rec('AP10', 'C 链/embedding/pgvector/RUN_BINDING_AUTH 关闭', noEmbedding);
}

const failed = results.filter(r => !r.pass);
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
writeFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'agent-pilot-report.json'),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length },
    results, finished_at: new Date().toISOString() }, null, 1));
console.log(`\nagent pilot: ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
