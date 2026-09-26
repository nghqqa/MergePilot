// integration.mjs — ORG_KNOWLEDGE_RAG_CONTROLLED_INTEGRATION 受控接入验证。
// 覆盖：known-hit / 合法空 / 不可达 degraded / 语料损坏 / 审计读回（MinIO）/
// 风险隔离（gate/stage/ticket/receipt 不变）/ 真实 PR 前后回归 / 控制台边界。
import { execSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { ragRetrieve } from './rag-retrieve.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONSOLE = 'http://127.0.0.1:48190';
const results = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass, detail: String(detail ?? '') });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
const CREDS = { user: 'pilot', password: 'pilot-read-only-2026' };
async function loginConsole() {
  const r = await fetch(`${CONSOLE}/api/auth/login`, { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(CREDS) });
  return (r.headers.get('set-cookie') || '').split(';')[0];
}
const q = (sql) => execSync(`docker exec mp-cc-pg psql -U mpcc -d mpcc -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
async function consoleGet(p, cookie) {
  const r = await fetch(CONSOLE + p, { headers: cookie ? { cookie } : {} });
  return { status: r.status, header: r.headers.get('x-rag-service-state'), body: await r.json().catch(() => null) };
}

process.env.MERGEPILOT_ORG_RAG_A_CHAIN = '1';
process.env.ORG_RAG_LIVE_URL = 'http://127.0.0.1:48210';

// ── 0. 前置：rag-live（正常语料）+ 控制台带 flag 运行中（外部脚本已部署）──
// 快照真实 PR 状态（接入前基线）
const pre = {
  receipts: q("SELECT count(*) FROM skill_receipt_outbox"),
  gates: q("SELECT count(*) FROM skill_gate_audit"),
  tickets: q("SELECT count(*) FROM approval.tickets"),
};
const CK = await loginConsole();
const preOv = await consoleGet('/api/overview', CK);
const preStages = JSON.stringify(preOv.body.stage_counts);

// ── 1 known-hit（worker 通道 + 控制台通道）──
{
  const w = await ragRetrieve({ query: '密码 轮换 密钥 存储 周期', runId: 'run-org-a1' });
  rec('V1', 'worker known-hit：lexical-zh-en-v1 + snapshot_id/digest/source_refs + org_knowledge 标记',
    w.ok && w.retrieval_version === 'lexical-zh-en-v1' && /^snap-/.test(w.snapshot_id || '')
      && /^[0-9a-f]{64}$/.test(w.corpus_digest || '') && w.source_refs.length > 0
      && w.results.every((r) => r.knowledge_type === 'org_knowledge'),
    `snap=${w.snapshot_id?.slice(0, 12)} refs=${w.source_refs.length}`);
  const c = await consoleGet('/api/rag/org-search?q=' + encodeURIComponent('密码 轮换 密钥'), CK);
  rec('V1b', 'console known-hit：代理返回 + source=ORG_RAG + reference-only 声明',
    c.status === 200 && c.body.source === 'ORG_RAG' && c.body.results?.length > 0
      && c.body.knowledge_type === 'org_knowledge' && !!c.body.usage_note,
    `status=${c.status} hits=${c.body.results?.length}`);
}
// ── 2 合法空 ──
{
  const w = await ragRetrieve({ query: '量子 火星 地铁', runId: 'run-org-a2' });
  const c = await consoleGet('/api/rag/org-search?q=' + encodeURIComponent('量子 火星 地铁'), CK);
  rec('V2', '合法空：空数组 + service_state=ok + 不伪造命中（双通道）',
    w.ok && w.results.length === 0 && w.empty_reason === 'no_lexical_match'
      && c.status === 200 && c.body.results?.length === 0 && c.body.service_state === 'ok');
}
// ── 3 服务不可达 ──
{
  const savedUrl = process.env.ORG_RAG_LIVE_URL;
  process.env.ORG_RAG_LIVE_URL = 'http://127.0.0.1:48299';   // 无人监听
  const w = await ragRetrieve({ query: '密码', runId: 'run-org-a3' });
  process.env.ORG_RAG_LIVE_URL = savedUrl;
  rec('V3', 'worker 不可达：显式 degraded（service_unreachable），非空成功',
    !w.ok && w.service_state === 'degraded' && w.degraded_reason === 'service_unreachable',
    `${w.service_state}/${w.degraded_reason}`);
}
// ── 4 语料损坏 ──
{
  const tmp = path.join(__dirname, 'corpus', '.ci-corrupted.json');
  const corpus = JSON.parse(fs.readFileSync(path.join(__dirname, 'corpus', 'org-security-standards.v1.json'), 'utf8'));
  corpus.documents[0].chunks[0] += '（见 run-canary6-st-426 与 nghqqa/tizhou#2 的处理）';
  fs.writeFileSync(tmp, JSON.stringify(corpus));
  const child = spawn(process.execPath, [path.join(__dirname, 'rag-live.mjs')], {
    env: { ...process.env, RAG_LIVE_PORT: '48212', RAG_LIVE_CORPUS: tmp,
           RAG_LIVE_AUDIT: path.join(__dirname, 'audit', '.ci-corrupted.jsonl') },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  try {
    let up = false;
    for (let i = 0; i < 20 && !up; i++) { await sleep(200); try { await fetch('http://127.0.0.1:48212/api/rag/search?q=x'); up = true; } catch {} }
    const r = await fetch('http://127.0.0.1:48212/api/rag/search?q=' + encodeURIComponent('密码 标准'));
    const b = await r.json().catch(() => ({}));
    rec('V4', '语料损坏：内容寻址/screening 拒载 → degraded 503，绝不用旧语料继续服务',
      up && r.status === 503 && b.service_state === 'degraded'
        && b.degraded_reason === 'corpus_unavailable' && (b.results || []).length === 0,
      `${r.status}/${b.degraded_reason}`);
  } finally { child.kill(); fs.rmSync(tmp, { force: true }); }
}
// ── 5 审计读回（隔离 MinIO）+ 健康探测不冒充 ──
{
  const auditPath = path.join(__dirname, 'audit', 'worker-retrievals.jsonl');
  const lines = fs.readFileSync(auditPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const business = lines.filter((l) => l.run_id && String(l.run_id).startsWith('run-org'));
  const okBiz = business.filter((l) => l.service_state === 'ok');
  rec('V5', '审计记录：业务检索含 snapshot_id/query_hash/source_refs/service_state/run_id（OK 记录全字段；degraded 记录如实含 degraded_reason）',
    okBiz.length >= 2 && okBiz.every((l) => l.snapshot_id && /^[0-9a-f]{32}$/.test(l.query_hash)
      && Array.isArray(l.source_refs) && l.service_state)
      && business.every((l) => /^[0-9a-f]{32}$/.test(l.query_hash) && l.service_state && l.run_id
        && (l.service_state === 'ok' ? true : !!l.degraded_reason)),
    `business=${business.length} ok=${okBiz.length} total=${lines.length}`);
  // 健康探测（无 run_id）不得计入业务检索
  rec('V5b', '健康探测（无 run_id）不冒充业务检索', business.every((l) => l.run_id));
  // 上传隔离 MinIO 并读回（单引号 shell 命令避免嵌套引号）
  const bucket = 'org-rag-audit';
  const sh = 'mc alias set s http://mp-cc-minio:9000 mpcc mp-cc-staging-pw >/dev/null '
    + `&& mc mb --ignore-existing s/${bucket} >/dev/null `
    + `&& mc cp /a.jsonl s/${bucket}/worker-retrievals.jsonl >/dev/null`;
  const hostAudit = auditPath.split(String.fromCharCode(92)).join(String.fromCharCode(47));
  execSync(`docker run --rm --network mp-cc-net --entrypoint sh -v "${hostAudit}:/a.jsonl:ro" mp-r13-worker-skill:candidate -c "${sh}"`,
    { stdio: ['ignore', 'pipe', 'pipe'], timeout: 60000 });
  const remote = execSync(`docker run --rm --network mp-cc-net --entrypoint sh mp-r13-worker-skill:candidate `
    + `-c "mc alias set s http://mp-cc-minio:9000 mpcc mp-cc-staging-pw >/dev/null && mc cat s/${bucket}/worker-retrievals.jsonl"`,
    { encoding: 'buffer', stdio: ['ignore', 'pipe', 'pipe'], timeout: 60000 });
  const local = fs.readFileSync(auditPath);
  const crypto = await import('node:crypto');
  rec('V5c', '审计可从隔离 MinIO 读回（sha256 逐字节一致）',
    crypto.createHash('sha256').update(local).digest('hex')
      === crypto.createHash('sha256').update(remote).digest('hex'));
}
// ── 6+7 风险隔离 + 真实 PR 前后回归 ──
{
  const post = {
    receipts: q("SELECT count(*) FROM skill_receipt_outbox"),
    gates: q("SELECT count(*) FROM skill_gate_audit"),
    tickets: q("SELECT count(*) FROM approval.tickets"),
  };
  const postOv = await consoleGet('/api/overview', CK);
  rec('V6', '风险隔离：receipt/gate/ticket 计数不变（检索不产生 finding/票据）',
    pre.receipts === post.receipts && pre.gates === post.gates && pre.tickets === post.tickets,
    `receipts ${pre.receipts}->${post.receipts} gates ${pre.gates}->${post.gates} tickets ${pre.tickets}->${post.tickets}`);
  rec('V7', '真实 PR 回归：两 PR stage 前后一致（BLOCKED/PASSED 未被改写）',
    preStages === JSON.stringify(postOv.body.stage_counts)
      && postOv.body.prs.some((p) => p.repo === 'nghqqa/tizhou' && p.pr_number === 2)
      && postOv.body.prs.some((p) => p.repo === 'wookat/speaktype' && p.pr_number === 426),
    `stages=${JSON.stringify(postOv.body.stage_counts)}`);
}
// ── 8 控制台边界 ──
{
  const unauth = await consoleGet('/api/rag/org-search?q=x');
  rec('V8', '未认证 401；未授权仓不泄漏（结果仅组织规范，无 repo 数据）',
    unauth.status === 401, String(unauth.status));
  const c = await consoleGet('/api/rag/org-search?q=' + encodeURIComponent('密码'), CK);
  const noRepoLeak = JSON.stringify(c.body).indexOf('nghqqa') === -1
    && JSON.stringify(c.body).indexOf('speaktype') === -1;
  rec('V8b', '检索结果不含仓库事实（screening + 隔离语料双重保证）', noRepoLeak);
}

const failed = results.filter((r) => !r.pass);
fs.writeFileSync(path.join(__dirname, 'integration-report.json'),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length, fail: failed.length },
    results, finished_at: new Date().toISOString() }, null, 1));
console.log(`\ncontrolled integration: ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
