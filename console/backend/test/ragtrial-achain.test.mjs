// ragtrial-achain.test.mjs — A 链 org-search × ragtrial 内部接线合同测试（feat/rag-integration）。
// 覆盖：flag off 行为保持 / backend_not_wired 如实 / PG 故障显式降级 / Review 安全边界
// （org-search 结果不可晋升 finding/ticket/gate、不可作 fixer 输入、Verifier 恒拒收）。
// hit/empty/index_stale 的正向映射在集成栈联调中实测（见 POST_PROMOTE_RAG_INTEGRATION_REPORT）。
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import { createConsole } from '../server.mjs';
import { toAuxEvidence, canAutoPromote, fixerPatchInputs, verifierAccepts, promotionRequest } from '../lib/ragtrial/review.mjs';

let BASE; let srv; let jar = {};
function setJar(res) { for (const c of res.headers.getSetCookie?.() ?? []) { const [kv] = c.split(';'); const [k, ...v] = kv.split('='); jar[k.trim()] = v.join('='); } }
const cookie = () => ({ cookie: Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ') });

before(async () => {
  Object.assign(process.env, {
    CONSOLE_SESSION_SECRET: 'rag-achain-test',
    CONSOLE_PILOT_USER: 'pilot', CONSOLE_PILOT_PASSWORD: 'test-password-pilot',
    MERGEPILOT_ORG_RAG_A_CHAIN: '1',
    MERGEPILOT_RAG_TRIAL_A_CHAIN: 'ragtrial',
    // 无 CONSOLE_PG_DSN → backend_not_wired（诚实）
  });
  delete process.env.CONSOLE_PG_DSN;
  const { server } = createConsole({ evidenceRoot: fs.mkdtempSync(os.tmpdir() + '/ev-') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  BASE = `http://127.0.0.1:${server.address().port}`;
  srv = server;
});
after(() => srv?.close());

test('未登录 401；flag=ragtrial 无 DSN → backend_not_wired 如实（不伪装命中）', async () => {
  assert.equal((await fetch(BASE + '/api/rag/org-search?q=x')).status, 401);
  setJar(await fetch(BASE + '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ user: 'pilot', password: 'test-password-pilot' }) }));
  const r = await fetch(BASE + '/api/rag/org-search?q=deploy', { headers: cookie() });
  assert.equal(r.status, 200);
  const b = await r.json();
  assert.equal(b.source, 'RAG_TRIAL');
  assert.equal(b.service_state, 'backend_not_wired');
  assert.deepEqual(b.results, []);
});

test('PG 不可达 → 503 显式降级（degraded + error_kind，不伪装空成功）', async () => {
  process.env.CONSOLE_PG_DSN = 'postgres://postgres:test-password-x@127.0.0.1:1/none';
  try {
    const r = await fetch(BASE + '/api/rag/org-search?q=deploy', { headers: cookie() });
    assert.equal(r.status, 503);
    const b = await r.json();
    assert.equal(b.service_state, 'degraded');
    assert.ok(b.degraded_reason);
    assert.deepEqual(b.results, []);
  } finally { delete process.env.CONSOLE_PG_DSN; }
});

// ── 词汇契约（P1 修复：hit/ok 统一）──
// A 链端点对"检索成功"只发射 'hit'：上游 org-rag 的 'ok' 在边界归一为 'hit'；
// 上游 degraded 原样透传为 503 degraded。前端只消费单一 'hit'（RagTrialPage）。
test('词汇契约：上游 org-rag ok → 端点归一为 hit；degraded 透传', async () => {
  const upstream = http.createServer((rq, rs) => {
    const ok = new URL(rq.url, 'http://x').searchParams.get('q') === 'okcase';
    rs.setHeader('content-type', 'application/json');
    rs.end(JSON.stringify(ok
      ? { service_state: 'ok', results: [], snapshot_id: 's1', knowledge_type: 'org_knowledge' }
      : { service_state: 'degraded', degraded_reason: 'stub_down' }));
  });
  await new Promise((r) => upstream.listen(0, '127.0.0.1', r));
  const prevFlag = process.env.MERGEPILOT_RAG_TRIAL_A_CHAIN;
  const prevUrl = process.env.ORG_RAG_LIVE_URL;
  process.env.MERGEPILOT_RAG_TRIAL_A_CHAIN = '';
  process.env.ORG_RAG_LIVE_URL = `http://127.0.0.1:${upstream.address().port}`;
  try {
    const r1 = await fetch(BASE + '/api/rag/org-search?q=okcase', { headers: cookie() });
    assert.equal(r1.status, 200);
    const b1 = await r1.json();
    assert.equal(b1.service_state, 'hit', "上游 'ok' 必须归一为 'hit'（词汇单值契约）");
    assert.equal(b1.source, 'ORG_RAG');
    assert.match(b1.usage_note ?? '', /reference only/);

    const r2 = await fetch(BASE + '/api/rag/org-search?q=other', { headers: cookie() });
    assert.equal(r2.status, 503);
    const b2 = await r2.json();
    assert.equal(b2.service_state, 'degraded');
    assert.equal(b2.degraded_reason, 'stub_down');
  } finally {
    process.env.MERGEPILOT_RAG_TRIAL_A_CHAIN = prevFlag;
    if (prevUrl) process.env.ORG_RAG_LIVE_URL = prevUrl; else delete process.env.ORG_RAG_LIVE_URL;
    upstream.close();
  }
});

test('Review 安全边界：org-search 形状的结果不可自动晋升/不可作 fixer 输入/Verifier 恒拒收', () => {
  // 与 org-search ragtrial 分支的实际返回形状一致（citation + reference_only）
  const orgHit = { score: 0.45, snippet: '部署前必须检查回滚锚点…',
    citation: { repo: 'nghqqa/mergepilot', branch: 'feat/local-rag-trial', doc_path: 'ops-runbook.md',
      line_start: 1, line_end: 24, doc_sha256: 'a'.repeat(64), chunk_sha256: 'b'.repeat(64),
      model_id: 'local-hash-v1', model_digest: '5'.repeat(64), index_version: 1 },
    reference_only: true };
  const ev = toAuxEvidence(orgHit, { runId: 'run-x' });
  assert.equal(ev.kind, 'rag_auxiliary');
  assert.equal(ev.trusted, false);
  assert.equal(ev.usage, 'reference_only');
  const ap = canAutoPromote(ev);
  assert.equal(ap.allowed, false);
  assert.ok(ap.reasons.includes('rag_evidence_is_reference_only'));
  for (const target of ['finding', 'ticket', 'gate']) {
    const r = promotionRequest({ target, evidence: ev });
    assert.equal(r.allowed, false, `${target} 必须拒绝：${r.reason}`);
    assert.match(r.reason, /rag_evidence_cannot_auto_promote/);
  }
  // fixer 输入剔除 RAG 后为空 → 不可启动（reason=机器码，中文解释在 note）
  const fx = fixerPatchInputs([ev]);
  assert.equal(fx.allowed.length, 0);
  assert.equal(fx.fixer_may_run, false);
  assert.equal(fx.reason, 'no_non_rag_evidence');
  assert.match(fx.note ?? '', /不得只依据 RAG/);
  // Verifier 只认独立证据
  assert.equal(verifierAccepts(ev).accepted, false);
  assert.equal(verifierAccepts({ kind: 'harness_report' }).accepted, true);
});
