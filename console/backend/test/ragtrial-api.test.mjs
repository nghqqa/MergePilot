// console/backend/test/ragtrial-api.test.mjs — ragtrial HTTP 层（无 PG）：
// 未登录 401；未配置 CONSOLE_PG_DSN → BACKEND_NOT_WIRED（不伪装检索）；
// A 链代理端点行为回归（未被 ragtrial 波改动）。
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createConsole } from '../server.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

process.env.CONSOLE_PILOT_USER = 'pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'pw-test';
process.env.CONSOLE_SESSION_SECRET = 'test-secret-ragtrial';
delete process.env.CONSOLE_PG_DSN;

async function withServer(fn) {
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try { await fn(base); } finally { server.close(); }
}

async function login(base) {
  const res = await fetch(base + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'pilot', password: 'pw-test' }),
  });
  assert.equal(res.status, 200, '登录必须成功');
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  // rc.10 SEC-3：rag-trial POST 强制 CSRF——从登录 Set-Cookie 串解析 mp_csrf
  const csrf = (res.headers.get('set-cookie') || '').match(/mp_csrf=([^;,]+)/)?.[1] ?? null;
  return { cookie, csrf };
}

test('rag-trial 未登录全端点 401', async () => {
  await withServer(async (base) => {
    for (const [method, p] of [
      ['GET', '/api/rag-trial/status'],
      ['POST', '/api/rag-trial/query'],
      ['POST', '/api/rag-trial/ingest'],
      ['POST', '/api/rag-trial/delete'],
      ['GET', '/api/rag-trial/metrics'],
      ['POST', '/api/rag-trial/eval'],
      ['POST', '/api/rag-trial/index/invalidate'],
      ['POST', '/api/rag-trial/index/rollback'],
      ['POST', '/api/rag-trial/review-aux'],
      ['POST', '/api/rag-trial/policy-check'],
    ]) {
      const res = await fetch(base + p, { method });
      assert.equal(res.status, 401, `${method} ${p} 必须 401`);
    }
  });
});

test('已登录 + 未接线（无 DSN）→ backend_not_wired 如实返回（不伪装检索）', async () => {
  await withServer(async (base) => {
    const { cookie, csrf } = await login(base);
    for (const [method, p, body] of [
      ['GET', '/api/rag-trial/status', null],
      ['POST', '/api/rag-trial/query', { q: 'x', repo: 'a/b', branch: 'main' }],
      ['POST', '/api/rag-trial/policy-check', { evidence: [] }],
    ]) {
      const res = await fetch(base + p, {
        method, headers: { cookie, 'content-type': 'application/json',
          ...(method === 'POST' && csrf ? { 'x-csrf-token': csrf } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      assert.equal(res.status, 200, p);
      const j = await res.json();
      assert.equal(j.service_state, 'backend_not_wired', `${p} 必须 BACKEND_NOT_WIRED`);
      assert.match(j.note, /CONSOLE_PG_DSN/);
    }
  });
});

test('A 链代理端点未被 ragtrial 改动（回归：flag off 如实 disabled）', async () => {
  await withServer(async (base) => {
    const saved = process.env.MERGEPILOT_ORG_RAG_A_CHAIN;
    delete process.env.MERGEPILOT_ORG_RAG_A_CHAIN;
    const { cookie } = await login(base);
    const res = await fetch(base + '/api/rag/org-search?q=sec', { headers: { cookie } });
    assert.equal(res.status, 200);
    const j = await res.json();
    assert.equal(j.service_state, 'a_chain_disabled', 'A 链 flag off 行为保持');
    if (saved !== undefined) process.env.MERGEPILOT_ORG_RAG_A_CHAIN = saved;
  });
});
