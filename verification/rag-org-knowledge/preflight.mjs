// preflight.mjs — A 链（worker rag_retrieve → 隔离 rag-live → 组织安全标准语料）
// 预检矩阵：已知命中 / 合法空 / 服务不可达 degraded / 语料损坏 fail-closed /
// 内容寻址与 screening / 审计关联 / 检索版本固定 / 核心面不受影响。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { loadCorpus, corpusDigest, snapshotId, screenCorpus, RETRIEVAL_VERSION } from './corpus-gate.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const results = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass, detail: String(detail ?? '') });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
const PORT = 48211;

async function startServer(extraEnv = {}) {
  const child = spawn(process.execPath, [path.join(__dirname, 'rag-live.mjs')], {
    env: { ...process.env, RAG_LIVE_PORT: String(PORT), ...extraEnv },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  for (let i = 0; i < 30; i++) {
    await sleep(200);
    try { await fetch(`http://127.0.0.1:${PORT}/api/rag/search?q=ping`); return child; }
    catch { /* booting */ }
  }
  throw new Error('rag-live did not start');
}
async function search(q, k = 5, run_id = null) {
  const r = await fetch(`http://127.0.0.1:${PORT}/api/rag/search?q=${encodeURIComponent(q)}&k=${k}${run_id ? `&run_id=${run_id}` : ''}`);
  return { status: r.status, state: r.headers.get('x-rag-service-state'), body: await r.json() };
}

// 1-2 内容寻址 + screening + 版本
{
  const meta = loadCorpus(path.join(__dirname, 'corpus', 'org-security-standards.v1.json'));
  rec('P1', '语料装载：shape+screening 通过；snapshot_id/digest 内容寻址',
    /^snap-[0-9a-f]{32}$/.test(meta.snapshot_id) && /^[0-9a-f]{64}$/.test(meta.digest),
    `${meta.snapshot_id}`);
  rec('P2', '检索版本固定 lexical-zh-en-v1', meta.retrieval_version === 'lexical-zh-en-v1'
    && RETRIEVAL_VERSION === 'lexical-zh-en-v1');
  const corpus = JSON.parse(fs.readFileSync(path.join(__dirname, 'corpus', 'org-security-standards.v1.json'), 'utf8'));
  const docs = corpus.documents.length;
  const chunks = corpus.documents.reduce((n, d) => n + d.chunks.length, 0);
  rec('P3', '记录完备：documents/chunks/source_ref/data_mode/publication_status',
    docs === 4 && chunks === 10 && corpus.documents.every((d) => d.source_ref)
      && corpus.data_mode === 'org_knowledge' && corpus.publication_status === 'published_internal',
    `docs=${docs} chunks=${chunks}`);
  // screening 负向：注入仓库事实必须被拒
  const bad = JSON.parse(JSON.stringify(corpus));
  bad.documents[0].chunks[0] += '（参考 nghqqa/fastapi-boilerplate-demo 的处理）';
  const screened = screenCorpus(bad);
  rec('P4', 'screening 负向：注入仓库事实 → 拒绝', !screened.ok && screened.findings.some((f) => f.rule === 'repo-fact'),
    JSON.stringify(screened.findings));
}

// 5-8 检索矩阵（含审计）
const AUDIT = path.join(__dirname, 'audit', 'retrievals.jsonl');
try { fs.rmSync(AUDIT, { force: true }); } catch {}
{
  const srv = await startServer();
  try {
    // 已知命中：密码标准
    const hit = await search('密码 轮换 密钥 存储', 5, 'run-preflight-a1');
    rec('P5', '已知命中：密码/密钥标准检索到 std-001',
      hit.status === 200 && hit.body.service_state === 'ok'
        && hit.body.results.length > 0 && hit.body.results[0].doc_id === 'std-001',
        `hits=${hit.body.results.length} top=${hit.body.results[0]?.doc_id}`);
    // 已知命中：安全审查人工门
    const hit2 = await search('高危 发现 人工 批准 合并', 5, 'run-preflight-a1');
    rec('P6', '已知命中：代码审查标准检索到 std-002',
      hit2.body.results.some((r) => r.doc_id === 'std-002'),
        `docs=${[...new Set(hit2.body.results.map((r) => r.doc_id))].join(',')}`);
    // 合法空：无匹配
    const empty = await search('量子纠缠 火星 地铁', 5, 'run-preflight-a2');
    rec('P7', '合法空结果：零命中如实返回（非降级）',
      empty.status === 200 && empty.body.service_state === 'ok'
        && empty.body.results.length === 0 && empty.body.empty_reason === 'no_lexical_match');
  } finally { srv.kill(); }

  // 6c 服务不可达：worker 视角必须 degraded，不伪装
  const down = await fetch(`http://127.0.0.1:${PORT}/api/rag/search?q=x`).catch((e) => ({ unreachable: true, error: e.code || String(e) }));
  rec('P8', '服务不可达：显式不可达（worker 侧标记 degraded 的前置事实）',
    !!down.unreachable, down.error || '');

  // 审计关联
  const lines = fs.readFileSync(AUDIT, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const driven = lines.filter((l) => l.run_id && l.run_id.startsWith('run-preflight'));
  rec('P9', '审计关联：预检驱动的每次检索含 snapshot_id/query_hash/source_refs/service_state/run_id',
    driven.length >= 3 && driven.every((l) => l.snapshot_id && /^[0-9a-f]{32}$/.test(l.query_hash)
      && Array.isArray(l.source_refs) && l.service_state === 'ok'),
    `driven=${driven.length} total=${lines.length}`);
}

// 6d 语料损坏 → fail-closed（服务启动但 degraded 503，绝不正常检索）
{
  const tmpCorpus = path.join(__dirname, 'corpus', '.corrupted-test.json');
  const corpus = JSON.parse(fs.readFileSync(path.join(__dirname, 'corpus', 'org-security-standards.v1.json'), 'utf8'));
  corpus.documents[1].chunks[0] = '含秘密 ghp_abcdefghijklmnopqrstuvwxyz1234567890 的损坏内容';
  fs.writeFileSync(tmpCorpus, JSON.stringify(corpus));
  const srv = await startServer({ RAG_LIVE_CORPUS: tmpCorpus, RAG_LIVE_AUDIT: path.join(__dirname, 'audit', '.corrupted.jsonl') });
  try {
    const r = await search('密码 标准', 5, 'run-preflight-a3');
    rec('P10', '语料损坏/敏感内容 → 拒绝装载，服务 degraded 503（不伪装正常检索）',
      r.status === 503 && r.body.service_state === 'degraded'
        && r.body.degraded_reason === 'corpus_unavailable' && r.body.results.length === 0,
        `${r.status}/${r.body.degraded_reason}`);
  } finally { srv.kill(); fs.rmSync(tmpCorpus, { force: true }); }
}

// 9 核心面不受影响（canonical console 两 PR 只读路径）
{
  const login = await fetch('http://127.0.0.1:48190/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'pilot', password: 'pilot-read-only-2026' }) });
  const ck = (login.headers.get('set-cookie') || '').split(';')[0];
  const pulls = await (await fetch('http://127.0.0.1:48190/api/pulls', { headers: { cookie: ck } })).json();
  const repos = new Set(pulls.pulls.map((p) => p.repo));
  rec('P11', '核心面不受影响：两授权 PR 只读路径正常',
    pulls.source === 'POSTGRESQL_LIVE' && repos.has('nghqqa/tizhou') && repos.has('wookat/speaktype'),
    `${pulls.source} repos=${repos.size}`);
}

const failed = results.filter((r) => !r.pass);
fs.writeFileSync(path.join(__dirname, 'preflight-report.json'),
  JSON.stringify({ summary: { total: results.length, pass: results.length - failed.length, fail: failed.length },
    results, finished_at: new Date().toISOString() }, null, 1));
console.log(`\nA-chain preflight: ${results.length - failed.length}/${results.length} PASS`);
if (failed.length) process.exit(1);
