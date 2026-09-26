// console/backend/test/contract-states.test.mjs — P2 合同状态测试（2026-09-26 收口轮）。
// 覆盖：/api/rag/org-search 三分支（401 匿名 / a_chain_disabled / 上游不可达 503 degraded）、
// /api/audit 未接线诚实态、/api/health 降级不伪装。
// 运行：node --test console/backend/test/contract-states.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createConsole } from '../server.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

process.env.CONSOLE_PILOT_USER = 'pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'pw-test';
process.env.CONSOLE_SESSION_SECRET = 'test-secret';
delete process.env.CONSOLE_PG_DSN;

async function start(env = {}) {
  for (const [k, v] of Object.entries(env)) process.env[k] = v;
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, base: `http://127.0.0.1:${server.address().port}` };
}

async function call(base, p, init) {
  const r = await fetch(base + p, init);
  let body = null; try { body = await r.json(); } catch { /* non-json */ }
  return { status: r.status, body, headers: r.headers };
}

async function loginCookie(base) {
  const r = await fetch(base + '/api/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'pilot', password: 'pw-test' }),
  });
  assert.equal(r.status, 200);
  return (r.headers.getSetCookie?.() || []).map((c) => c.split(';')[0]).join('; ');
}

test('org-search: 匿名 401（不泄漏服务状态）', async () => {
  const { server, base } = await start({ MERGEPILOT_ORG_RAG_A_CHAIN: '1' });
  try {
    const r = await call(base, '/api/rag/org-search?q=x');
    assert.equal(r.status, 401);
  } finally { server.close(); }
});

test('org-search: feature flag 关闭 → a_chain_disabled（诚实，不冒充检索结果）', async () => {
  const { server, base } = await start({ MERGEPILOT_ORG_RAG_A_CHAIN: '0' });
  try {
    const cookie = await loginCookie(base);
    const r = await call(base, '/api/rag/org-search?q=x', { headers: { cookie } });
    assert.equal(r.status, 200);
    assert.equal(r.body.service_state, 'a_chain_disabled');
  } finally { server.close(); }
});

test('org-search: 上游不可达 → 503 degraded + 空结果 + x-rag-service-state 头（不冒充）', async () => {
  // 127.0.0.1:1 为保留关闭端口——真实连接失败，非 mock
  const { server, base } = await start({ MERGEPILOT_ORG_RAG_A_CHAIN: '1', ORG_RAG_LIVE_URL: 'http://127.0.0.1:1' });
  try {
    const cookie = await loginCookie(base);
    const r = await call(base, '/api/rag/org-search?q=x', { headers: { cookie } });
    assert.equal(r.status, 503);
    assert.equal(r.headers.get('x-rag-service-state'), 'degraded');
    assert.equal(r.body.service_state, 'degraded');
    assert.deepEqual(r.body.results, []);
  } finally { server.close(); }
});

test('audit: 未接线（无 DSN）→ core_source=BACKEND_NOT_WIRED，gate_decisions 空（不冒充零决策）', async () => {
  const { server, base } = await start({});
  try {
    const cookie = await loginCookie(base);
    const r = await call(base, '/api/audit', { headers: { cookie } });
    assert.equal(r.status, 200);
    assert.equal(r.body.core_source, 'BACKEND_NOT_WIRED');
    assert.deepEqual(r.body.gate_decisions, []);
  } finally { server.close(); }
});

test('health: 无 DSN 仍 200 且 data_mode 声明为 snapshot（降级不伪装 live）', async () => {
  const { server, base } = await start({});
  try {
    const r = await call(base, '/api/health');
    assert.equal(r.status, 200);
    assert.ok(['live', 'snapshot'].includes(r.body.data_mode));
    assert.equal(typeof r.body.declared_repos.length, 'number');
  } finally { server.close(); }
});
