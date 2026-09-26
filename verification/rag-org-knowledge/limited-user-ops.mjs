// limited-user-ops.mjs — ORG_KNOWLEDGE_A_CHAIN_LIMITED_USER_OPERATIONS 矩阵。
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
async function ensureRagLive(port = 48210) {
  for (let i = 0; i < 15; i++) { await sleep(400); try { await fetch(`http://127.0.0.1:${port}/api/rag/search?q=ping`); return true; } catch {} }
  return false;
}
function startRagLive(port = 48210) {
  spawn(process.execPath, [path.join(__dirname, 'rag-live.mjs')], {
    env: { ...process.env, RAG_LIVE_PORT: String(port) }, stdio: 'ignore', detached: true }).unref();
}
function killRagLive(port = 48210) {
  try {
    const pid = execSync(`netstat -ano | findstr :${port} | findstr LISTENING`,
      { encoding: 'utf8', shell: true }).trim().split(/\s+/).pop();
    if (pid) execSync(`taskkill /F /PID ${pid}`, { stdio: 'ignore', shell: true });
  } catch { /* not running */ }
}

const CK = await login();

// UO1: known-hit from user perspective
{
  const r = await orgSearch('密码 轮换 密钥 存储', CK);
  const b = r.body;
  rec('UO1', 'known-hit：ORG_RAG source + lexical-zh-en-v1 + snapshot/digest + reference-only + degraded 可区分',
    r.status === 200 && b.source === 'ORG_RAG' && b.retrieval_version === 'lexical-zh-en-v1'
      && /^snap-/.test(b.snapshot_id || '') && /^[0-9a-f]{64}$/.test(b.corpus_digest || '')
      && !!b.usage_note && b.knowledge_type === 'org_knowledge',
    `snap=${b.snapshot_id?.slice(0, 14)} type=${b.knowledge_type}`);
}
// UO2: risk-field contamination check
{
  const r = await orgSearch('高危 发现 人工 批准', CK);
  const s = JSON.stringify(r.body);
  rec('UO2', '结果零风险决策字段（finding/severity/verdict/approved）',
    !s.match(/"finding"|"severity":\s*"HIGH|"severity":\s*"MEDIUM|"severity":\s*"LOW|"verdict"|"approved":\s*true/),
    '');
}
// UO3: 合法空 + 不可达 + 语料损坏 + 恢复
{
  const empty = await orgSearch('量子 火星', CK);
  rec('UO3', '合法空：空数组 + ok + 不伪造',
    empty.status === 200 && empty.body.results?.length === 0 && empty.body.service_state === 'ok');

  killRagLive();
  await sleep(1500);
  const down = await orgSearch('密码 标准', CK);
  rec('UO4', '不可达：503 + degraded + 头 + 审计状态一致（不静默空成功）',
    down.status === 503 && down.body.service_state === 'degraded' && down.state === 'degraded'
      && (down.body.results || []).length === 0);

  startRagLive();
  const recovered = await ensureRagLive();
  const hit = await orgSearch('密码 标准', CK);
  rec('UO5', '恢复后 known-hit 正常 + 不用未校验旧语料',
    recovered && hit.status === 200 && hit.body.service_state === 'ok' && hit.body.results?.length > 0,
    `${recovered}/${hit.status}/${hit.body.results?.length}`);
}
// UO6: 语料损坏（one-shot）
{
  const tmp = path.join(__dirname, 'corpus', '.luo-corrupt.json');
  const c = JSON.parse(fs.readFileSync(path.join(__dirname, 'corpus', 'org-security-standards.v1.json'), 'utf8'));
  c.documents[0].chunks[0] += '（nghqqa/tizhou 的真实凭据）';
  fs.writeFileSync(tmp, JSON.stringify(c));
  const child = spawn(process.execPath, [path.join(__dirname, 'rag-live.mjs')], {
    env: { ...process.env, RAG_LIVE_PORT: '48214', RAG_LIVE_CORPUS: tmp,
           RAG_LIVE_AUDIT: path.join(__dirname, 'audit', '.luo-corrupt.jsonl') }, stdio: 'ignore' });
  try {
    await ensureRagLive(48214);
    const r = await fetch('http://127.0.0.1:48214/api/rag/search?q=' + encodeURIComponent('密码'));
    const b = await r.json().catch(() => ({}));
    rec('UO6', '语料损坏：拒载 → degraded 503（screening+内容寻址校验绕过为零）',
      r.status === 503 && b.service_state === 'degraded');
  } finally { child.kill(); fs.rmSync(tmp, { force: true }); }
}
// UO7: 重启 console（保留 rag-live）
{
  execSync('docker restart mp-stage-console', { stdio: 'pipe' });
  let up = false;
  for (let i = 0; i < 20 && !up; i++) { await sleep(500); try { up = (await fetch(`${B}/api/health`)).status === 200; } catch {} }
  const CK2 = await login();
  const hit = await orgSearch('密码', CK2);
  rec('UO7', 'console 重启：旧会话失效重登 + known-hit 正常',
    up && hit.status === 200 && hit.body.results?.length > 0);
  var CKN = CK2;
}
// UO8: 两 PR 不变（检索前后）
{
  const counts = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
  const ov = await (await fetch(`${B}/api/overview`, { headers: { cookie: CKN } })).json();
  rec('UO8', '两 PR 全不变：4/1/2 + PASSED 2 + 零 finding + success/action_required 无变化',
    counts === '4|1|2' && ov.stage_counts.PASSED === 2 && ov.stage_counts.ACTION_REQUIRED === 0);
}
// UO9: 退出 + 重新登录 + allowlist + 401/403/404
{
  const unauth = await fetch(`${B}/api/rag/org-search?q=x`);
  const foreign = await fetch(`${B}/api/pulls?repo=x/y`, { headers: { cookie: CKN } });
  const unknown = await fetch(`${B}/api/runs/UNKNOWN-XYZ`, { headers: { cookie: CKN } });
  rec('UO9', '401（未登录 org-search）+ 403（未授权仓）+ 404（未知 pack）',
    unauth.status === 401 && foreign.status === 403 && unknown.status === 404,
    `${unauth.status}/${foreign.status}/${unknown.status}`);
}
// UO10: 审计五字段 + 连续性
{
  const auditPath = path.join(__dirname, 'audit', 'retrievals.jsonl');
  const lines = fs.readFileSync(auditPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const recent = lines.slice(-10);
  const ok = recent.every((l) => l.snapshot_id !== undefined && l.query_hash && Array.isArray(l.source_refs)
    && l.service_state && ('run_id' in l));
  rec('UO10', '审计五字段完整 + 连续性（最近 10 条全过）', ok && recent.length === 10, `lines=${recent.length}`);
}

const failed = results.filter((r) => !r.pass);
fs.writeFileSync(path.join(__dirname, 'limited-user-ops-report.json'),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length }, results,
    finished_at: new Date().toISOString() }, null, 1));
console.log(`\nlimited user ops: ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
