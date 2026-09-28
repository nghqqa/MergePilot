// console/backend/test/ragtrial-authz.test.mjs — PHASE0A 会话仓库授权（无 PG 单测）：
// 纯函数语义（sessionRepoAllowed 默认拒绝）+ HTTP 未登录 401 面补齐（jobs/requeue/
// queue-metrics/models）+ legacy 未配置 allowlist 时会话 repos 为空（RAG 会话面
// 默认拒的可观测根因）。完整组合门行为（scope→会话两层、防泄露、零写副作用、
// 机器通道对照）由 ragtrial-canonical.integration.mjs 的 SA* 场景在真实 PG 上覆盖。
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createConsole } from '../server.mjs';
import { sessionRepoAllowed } from '../lib/ragtrial/api.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

process.env.CONSOLE_PILOT_USER = 'pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'authz-test-password'; // 合成占位值（含 secret-scan 豁免词表 token，非真实凭据）
process.env.CONSOLE_SESSION_SECRET = 'test-secret-authz';
delete process.env.CONSOLE_PG_DSN;
delete process.env.CONSOLE_REPO_ALLOWLIST;

test('sessionRepoAllowed：会话 repo 域判定（默认拒绝）', () => {
  assert.equal(sessionRepoAllowed({ repos: ['a/b', 'c/d'] }, 'a/b'), true);
  assert.equal(sessionRepoAllowed({ repos: ['a/b'] }, 'x/y'), false); // 域外拒绝
  assert.equal(sessionRepoAllowed({ repos: [] }, 'a/b'), false); // 空授权面=默认拒
  assert.equal(sessionRepoAllowed({}, 'a/b'), false); // 无 repos 字段=默认拒
  assert.equal(sessionRepoAllowed(null, 'a/b'), false); // 无会话=默认拒
  assert.equal(sessionRepoAllowed({ repos: ['a/b'] }, ''), false); // 空 repo 拒绝
});

test('legacy 登录未配置 allowlist → 会话 repos 为空（RAG 会话面默认拒的根因可观测）', async () => {
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const login = await fetch(base + '/api/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ user: 'pilot', password: 'authz-test-password' }),
    });
    assert.equal(login.status, 200, '登录必须成功');
    const cookie = (login.headers.get('set-cookie') || '').split(';')[0];
    const sess = await fetch(base + '/api/auth/session', { headers: { cookie } });
    const j = await sess.json();
    assert.deepEqual(j.repos, [], '未配置 allowlist 的会话授权面必须为空（默认拒）');
    // 无 DSN：已登录请求仍如实 backend_not_wired（未接线不伪装——既有语义保持）
    const q = await fetch(base + '/api/rag-trial/query', {
      method: 'POST', headers: { cookie, 'content-type': 'application/json' },
      body: JSON.stringify({ q: 'x', repo: 'a/b', branch: 'main' }),
    });
    assert.equal(q.status, 200);
    const qj = await q.json();
    assert.equal(qj.service_state, 'backend_not_wired');
  } finally { server.close(); }
});

test('会话端 RAG 端点未登录 401（PHASE0A 面补齐：jobs/requeue/queue-metrics/models）', async () => {
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    for (const [method, p] of [
      ['GET', '/api/rag-trial/jobs'],
      ['POST', '/api/rag-trial/jobs'],
      ['POST', '/api/rag-trial/jobs/00000000-0000-0000-0000-000000000000/requeue'],
      ['GET', '/api/rag-trial/queue/metrics'],
      ['POST', '/api/rag-trial/models'],
    ]) {
      const res = await fetch(base + p, { method });
      assert.equal(res.status, 401, `${method} ${p} 必须 401`);
    }
  } finally { server.close(); }
});
